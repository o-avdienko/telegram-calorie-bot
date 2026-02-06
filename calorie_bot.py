import os
import io
import requests
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Tuple
from dotenv import load_dotenv
from functools import lru_cache

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import asyncio


load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
WEATHER_API_KEY = os.getenv('WEATHER_API_KEY')

if not BOT_TOKEN:
    raise ValueError('BOT_TOKEN не найден в переменных окружения')
if not WEATHER_API_KEY:
    raise ValueError('WEATHER_API_KEY не найден в переменных окружения')

# Инициализация бота
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())



class CommandLogger(BaseMiddleware):
    """Логирует все команды пользователей"""
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.text:
            user = event.from_user
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{timestamp}] User {user.id} (@{user.username}): {event.text}")
        return await handler(event, data)


dp.message.middleware(CommandLogger())



@dataclass
class UserProfile:
    """Профиль пользователя с метриками здоровья"""
    weight: float
    height: int
    age: int
    gender: str
    activity_minutes: int
    city: str
    temperature: float | None = None
    
    # Текущие метрики
    water_consumed: int = 0
    calories_eaten: int = 0
    calories_burned: int = 0
    
    # Целевые значения
    water_target: int = 0
    calorie_target: int = 0
    
    # История для графиков (время, значение)
    water_timeline: List[Tuple[str, int]] = field(default_factory=list)
    calorie_timeline: List[Tuple[str, int]] = field(default_factory=list)
    workout_timeline: List[Tuple[str, int]] = field(default_factory=list)


# Хранилище профилей
user_database: dict[int, UserProfile] = {}



def current_time() -> str:
    """Возвращает текущее время в формате HH:MM"""
    return datetime.now().strftime("%H:%M")


def init_timeline(profile: UserProfile):
    """Инициализирует таймлайны если их нет"""
    if not profile.water_timeline:
        profile.water_timeline.append((current_time(), 0))
    if not profile.calorie_timeline:
        profile.calorie_timeline.append((current_time(), 0))
    if not profile.workout_timeline:
        profile.workout_timeline.append((current_time(), 0))


# === Расчёты норм ===
def calculate_water_norm(weight: float, activity: int, temp: float | None) -> int:
    """
    Расчёт дневной нормы воды.
    
    Формула:
    - Базовая норма: вес * 30 мл
    - Бонус за активность: +500 мл за каждые 30 минут
    - Коррекция на жару: +500 мл при температуре > 25°C
    """
    base_amount = weight * 30
    activity_bonus = (activity // 30) * 500
    heat_bonus = 500 if (temp and temp > 25) else 0
    
    return int(base_amount + activity_bonus + heat_bonus)


def calculate_calorie_norm(weight: float, height: int, age: int, gender: str, 
                          activity: int, manual: int | None = None) -> int:
    """
    Расчёт дневной нормы калорий по формуле Миффлина-Сан Жеора.
    
    BMR = 10*вес + 6.25*рост - 5*возраст + коррекция_пола
    Итоговая норма = BMR * коэффициент_активности
    """
    if manual:
        return manual
    
    # Базовый метаболизм
    bmr = 10 * weight + 6.25 * height - 5 * age
    bmr += 5 if gender == "male" else -161
    
    # Коэффициент активности
    if activity >= 60:
        multiplier = 1.55  # высокая активность
    elif activity >= 30:
        multiplier = 1.375  # умеренная активность
    else:
        multiplier = 1.2  # низкая активность
    
    return int(bmr * multiplier)



class WorkoutCalculator:
    """Расчёт сожжённых калорий на основе MET (метаболический эквивалент)"""
    
    MET_VALUES = {
        "бег": 10.0,
        "run": 10.0,
        "ходьба": 4.5,
        "walk": 4.5,
        "велосипед": 8.0,
        "cycling": 8.0,
        "плавание": 9.5,
        "swimming": 9.5,
        "зал": 6.5,
        "gym": 6.5,
        "йога": 3.5,
        "yoga": 3.5,
    }
    
    @classmethod
    def calculate_burned(cls, exercise: str, minutes: int, weight_kg: float) -> int:
        """
        Формула: калории = MET * вес(кг) * время(часы)
        """
        met = cls.MET_VALUES.get(exercise.lower(), 7.0)
        hours = minutes / 60.0
        return int(met * weight_kg * hours)
    
    @classmethod
    def water_bonus(cls, minutes: int) -> int:
        """Дополнительная вода после тренировки"""
        return (minutes // 30) * 200



def fetch_weather(city: str) -> float | None:
    """Получение температуры через OpenWeatherMap API"""
    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "q": city,
        "appid": WEATHER_API_KEY,
        "units": "metric",
        "lang": "ru"
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return float(data["main"]["temp"])
    except Exception as e:
        print(f"Ошибка погоды: {e}")
    
    return None



@lru_cache(maxsize=128)
def search_food_calories(query: str) -> tuple[str, float] | None:
    """
    Поиск калорийности продукта через OpenFoodFacts API.
    Возвращает (название, ккал_на_100г) или None.
    """
    url = "https://world.openfoodfacts.org/cgi/search.pl"
    params = {
        "search_terms": query,
        "json": 1,
        "page_size": 8,
        "fields": "product_name,nutriments"
    }
    
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code != 200:
            return None
        
        data = response.json()
        products = data.get("products", [])
        
        for product in products:
            nutrients = product.get("nutriments", {})
            
            # Попытка 1: прямое значение kcal
            if kcal := nutrients.get("energy-kcal_100g"):
                name = product.get("product_name") or query
                return (name, float(kcal))
            
            # Попытка 2: конвертация из kJ
            if kj := nutrients.get("energy_100g"):
                name = product.get("product_name") or query
                kcal_converted = float(kj) / 4.184
                return (name, kcal_converted)
        
        return None
    except Exception as e:
        print(f"Ошибка поиска продукта: {e}")
        return None





def parse_number(text: str, allow_float: bool = False) -> float | int | None:
    """Парсинг числа из текста"""
    try:
        text = text.replace(",", ".").strip()
        return float(text) if allow_float else int(float(text))
    except:
        return None



class ProfileSetup(StatesGroup):
    weight_input = State()
    height_input = State()
    age_input = State()
    gender_input = State()
    activity_input = State()
    city_input = State()
    manual_calories_choice = State()
    manual_calories_input = State()



@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    welcome_text = (
        "👋 Привет! Я твой персональный ассистент по здоровью.\n\n"
        "Что я умею:\n"
        "✅ Рассчитывать норму воды и калорий\n"
        "✅ Отслеживать питание и тренировки\n"
        "✅ Строить графики прогресса\n"
        "✅ Давать персональные рекомендации\n\n"
        "📋 Основные команды:\n"
        "/setup — настроить профиль\n"
        "/drink — добавить воду\n"
        "/eat — добавить еду\n"
        "/train — добавить тренировку\n"
        "/status — проверить прогресс\n"
        "/charts — графики за день\n"
        "/tips — получить рекомендации\n"
        "/reset — сбросить дневные данные\n\n"
        "Начни с команды /setup 🚀"
    )
    await message.answer(welcome_text)



@dp.message(Command("setup"))
async def cmd_setup(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "🔧 Настройка профиля\n\n"
        "Шаг 1/7: Укажи свой вес в килограммах\n"
        "Например: 75\n\n"
        "Отмена: /cancel"
    )
    await state.set_state(ProfileSetup.weight_input)


@dp.message(ProfileSetup.weight_input, ~F.text.startswith("/"))
async def process_weight(message: Message, state: FSMContext):
    weight = parse_number(message.text, allow_float=True)
    
    if not weight or weight < 30 or weight > 300:
        await message.answer("⚠️ Вес должен быть от 30 до 300 кг. Попробуй ещё раз:")
        return
    
    await state.update_data(weight=weight)
    await message.answer("Шаг 2/7: Укажи свой рост в сантиметрах\nНапример: 175")
    await state.set_state(ProfileSetup.height_input)


@dp.message(ProfileSetup.height_input, ~F.text.startswith("/"))
async def process_height(message: Message, state: FSMContext):
    height = parse_number(message.text)
    
    if not height or height < 100 or height > 250:
        await message.answer("⚠️ Рост должен быть от 100 до 250 см. Попробуй ещё раз:")
        return
    
    await state.update_data(height=height)
    await message.answer("Шаг 3/7: Укажи свой возраст в годах\nНапример: 25")
    await state.set_state(ProfileSetup.age_input)


@dp.message(ProfileSetup.age_input, ~F.text.startswith("/"))
async def process_age(message: Message, state: FSMContext):
    age = parse_number(message.text)
    
    if not age or age < 10 or age > 100:
        await message.answer("⚠️ Возраст должен быть от 10 до 100 лет. Попробуй ещё раз:")
        return
    
    await state.update_data(age=age)
    
    # Кнопки для выбора пола
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Мужской"), KeyboardButton(text="Женский")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("Шаг 4/7: Укажи свой пол", reply_markup=keyboard)
    await state.set_state(ProfileSetup.gender_input)


@dp.message(ProfileSetup.gender_input, ~F.text.startswith("/"))
async def process_gender(message: Message, state: FSMContext):
    gender_text = message.text.strip().lower()
    
    if "муж" in gender_text or "male" in gender_text:
        gender = "male"
    elif "жен" in gender_text or "female" in gender_text:
        gender = "female"
    else:
        await message.answer("⚠️ Выбери пол с помощью кнопок или напиши 'мужской'/'женский'")
        return
    
    await state.update_data(gender=gender)
    await message.answer(
        "Шаг 5/7: Укажи среднюю активность в минутах за день\n"
        "Например: 45\n\n"
        "Считается любая физическая активность: ходьба, спорт, зарядка",
        reply_markup=ReplyKeyboardRemove()
    )
    await state.set_state(ProfileSetup.activity_input)


@dp.message(ProfileSetup.activity_input, ~F.text.startswith("/"))
async def process_activity(message: Message, state: FSMContext):
    activity = parse_number(message.text)
    
    if activity is None or activity < 0 or activity > 500:
        await message.answer("⚠️ Активность должна быть от 0 до 500 минут. Попробуй ещё раз:")
        return
    
    await state.update_data(activity=activity)
    await message.answer(
        "Шаг 6/7: Укажи свой город\n"
        "Например: Moscow или London\n\n"
        "Это нужно для учёта погоды при расчёте нормы воды"
    )
    await state.set_state(ProfileSetup.city_input)


@dp.message(ProfileSetup.city_input, ~F.text.startswith("/"))
async def process_city(message: Message, state: FSMContext):
    city = message.text.strip()
    
    if len(city) < 2:
        await message.answer("⚠️ Название города слишком короткое. Попробуй ещё раз:")
        return
    
    await state.update_data(city=city)
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Да"), KeyboardButton(text="Нет")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer(
        "Шаг 7/7: Хочешь задать норму калорий вручную?\n"
        "(По умолчанию рассчитаю автоматически)",
        reply_markup=keyboard
    )
    await state.set_state(ProfileSetup.manual_calories_choice)


@dp.message(ProfileSetup.manual_calories_choice, ~F.text.startswith("/"))
async def process_manual_choice(message: Message, state: FSMContext):
    answer = message.text.strip().lower()
    
    if "да" in answer or "yes" in answer:
        await message.answer(
            "Введи желаемую норму калорий (ккал/день)\n"
            "Например: 2200",
            reply_markup=ReplyKeyboardRemove()
        )
        await state.set_state(ProfileSetup.manual_calories_input)
    elif "нет" in answer or "no" in answer:
        await finalize_profile(message, state, manual_calories=None)
    else:
        await message.answer("⚠️ Ответь 'Да' или 'Нет' с помощью кнопок")


@dp.message(ProfileSetup.manual_calories_input, ~F.text.startswith("/"))
async def process_manual_calories(message: Message, state: FSMContext):
    calories = parse_number(message.text)
    
    if not calories or calories < 1000 or calories > 5000:
        await message.answer("⚠️ Норма калорий должна быть от 1000 до 5000. Попробуй ещё раз:")
        return
    
    await finalize_profile(message, state, manual_calories=calories)


async def finalize_profile(message: Message, state: FSMContext, manual_calories: int | None):
    """Финализация настройки профиля"""
    data = await state.get_data()
    
    # Получение температуры
    temp = fetch_weather(data["city"])
    
    # Расчёт норм
    water_norm = calculate_water_norm(data["weight"], data["activity"], temp)
    calorie_norm = calculate_calorie_norm(
        data["weight"], data["height"], data["age"], 
        data["gender"], data["activity"], manual_calories
    )
    
    # Создание профиля
    profile = UserProfile(
        weight=data["weight"],
        height=data["height"],
        age=data["age"],
        gender=data["gender"],
        activity_minutes=data["activity"],
        city=data["city"],
        temperature=temp,
        water_target=water_norm,
        calorie_target=calorie_norm
    )
    
    init_timeline(profile)
    user_database[message.from_user.id] = profile
    
    temp_text = f"{temp:.1f}°C" if temp else "не определена"
    
    await message.answer(
        f"✅ Профиль успешно настроен!\n\n"
        f"📍 Город: {data['city']} (температура: {temp_text})\n"
        f"💧 Норма воды: {water_norm} мл/день\n"
        f"🔥 Норма калорий: {calorie_norm} ккал/день\n\n"
        f"Теперь можешь:\n"
        f"/drink — добавить воду\n"
        f"/eat — добавить еду\n"
        f"/train — добавить тренировку\n"
        f"/status — проверить прогресс",
        reply_markup=ReplyKeyboardRemove()
    )
    
    await state.clear()


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if not current_state:
        await message.answer("❌ Нечего отменять", reply_markup=ReplyKeyboardRemove())
        return
    
    await state.clear()
    await message.answer("✅ Действие отменено", reply_markup=ReplyKeyboardRemove())


def get_profile(user_id: int) -> UserProfile | None:
    return user_database.get(user_id)


def require_profile_message() -> str:
    return "⚠️ Сначала настрой профиль: /setup"


@dp.message(Command("drink"))
async def cmd_drink(message: Message):
    profile = get_profile(message.from_user.id)
    if not profile:
        await message.answer(require_profile_message())
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Укажи количество воды в мл\nПример: /drink 300")
        return
    
    amount = parse_number(parts[1])
    if not amount or amount <= 0 or amount > 3000:
        await message.answer("⚠️ Количество должно быть от 1 до 3000 мл")
        return
    
    profile.water_consumed += amount
    profile.water_timeline.append((current_time(), profile.water_consumed))
    
    remaining = max(profile.water_target - profile.water_consumed, 0)
    percent = min(int(profile.water_consumed / profile.water_target * 100), 100)
    
    await message.answer(
        f"💧 Добавлено: {amount} мл\n\n"
        f"Выпито за день: {profile.water_consumed} / {profile.water_target} мл ({percent}%)\n"
        f"Осталось: {remaining} мл"
    )


class FoodLogging(StatesGroup):
    waiting_grams = State()


@dp.message(Command("eat"))
async def cmd_eat(message: Message, state: FSMContext):
    profile = get_profile(message.from_user.id)
    if not profile:
        await message.answer(require_profile_message())
        return
    
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Укажи название продукта\n"
            "Пример: /eat apple\n"
            "или: /eat банан"
        )
        return
    
    product_query = parts[1].strip()
    result = search_food_calories(product_query)
    
    if not result:
        await message.answer(
            f"❌ Продукт '{product_query}' не найден\n\n"
            "Попробуй другое название (лучше на английском)\n"
            "Примеры: banana, chicken breast, rice"
        )
        return
    
    product_name, kcal_per_100g = result
    
    await state.update_data(
        product_name=product_name,
        kcal_per_100g=kcal_per_100g
    )
    
    await message.answer(
        f"🍎 Найдено: {product_name}\n"
        f"Калорийность: {kcal_per_100g:.1f} ккал на 100г\n\n"
        "Сколько грамм съел(а)?\n"
        "Например: 150\n\n"
        "Отмена: /cancel"
    )
    await state.set_state(FoodLogging.waiting_grams)


@dp.message(FoodLogging.waiting_grams, ~F.text.startswith("/"))
async def process_food_grams(message: Message, state: FSMContext):
    profile = get_profile(message.from_user.id)
    if not profile:
        await state.clear()
        await message.answer(require_profile_message())
        return
    
    grams = parse_number(message.text)
    if not grams or grams <= 0 or grams > 2000:
        await message.answer("⚠️ Граммы должны быть от 1 до 2000. Попробуй ещё раз:")
        return
    
    data = await state.get_data()
    product_name = data["product_name"]
    kcal_per_100g = data["kcal_per_100g"]
    
    total_kcal = (kcal_per_100g * grams) / 100.0
    profile.calories_eaten += int(total_kcal)
    profile.calorie_timeline.append((current_time(), profile.calories_eaten))
    
    balance = profile.calories_eaten - profile.calories_burned
    remaining = max(profile.calorie_target - balance, 0)
    percent = min(int(balance / profile.calorie_target * 100), 100)
    
    await message.answer(
        f"✅ Записано: {product_name}\n"
        f"Количество: {grams}г\n"
        f"Калории: +{int(total_kcal)} ккал\n\n"
        f"Съедено за день: {profile.calories_eaten} ккал\n"
        f"Баланс: {balance} / {profile.calorie_target} ккал ({percent}%)\n"
        f"Осталось: {remaining} ккал"
    )
    
    await state.clear()



@dp.message(Command("train"))
async def cmd_train(message: Message):
    profile = get_profile(message.from_user.id)
    if not profile:
        await message.answer(require_profile_message())
        return
    
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer(
            "Формат: /train <тип> <минуты>\n\n"
            "Примеры:\n"
            "/train бег 30\n"
            "/train walk 45\n"
            "/train зал 60\n\n"
            "Доступные типы: бег, ходьба, велосипед, плавание, зал, йога"
        )
        return
    
    exercise_type = parts[1].strip()
    duration = parse_number(parts[2])
    
    if not duration or duration <= 0 or duration > 300:
        await message.answer("⚠️ Длительность должна быть от 1 до 300 минут")
        return
    
    burned = WorkoutCalculator.calculate_burned(exercise_type, duration, profile.weight)
    water_bonus = WorkoutCalculator.water_bonus(duration)
    
    profile.calories_burned += burned
    profile.water_target += water_bonus
    profile.workout_timeline.append((current_time(), profile.calories_burned))
    
    balance = profile.calories_eaten - profile.calories_burned
    
    await message.answer(
        f"🏋️ Тренировка записана!\n\n"
        f"Тип: {exercise_type}\n"
        f"Длительность: {duration} мин\n"
        f"Сожжено: {burned} ккал\n\n"
        f"💧 Норма воды увеличена на {water_bonus} мл\n"
        f"Новая норма: {profile.water_target} мл\n\n"
        f"⚖️ Баланс калорий: {balance} ккал"
    )


@dp.message(Command("status"))
async def cmd_status(message: Message):
    profile = get_profile(message.from_user.id)
    if not profile:
        await message.answer(require_profile_message())
        return
    
    # Вода
    water_percent = min(int(profile.water_consumed / profile.water_target * 100), 100)
    water_remain = max(profile.water_target - profile.water_consumed, 0)
    
    # Калории
    calorie_balance = profile.calories_eaten - profile.calories_burned
    calorie_percent = min(int(calorie_balance / profile.calorie_target * 100), 100)
    calorie_remain = max(profile.calorie_target - calorie_balance, 0)
    
    # Эмодзи-прогресс
    def progress_bar(percent: int) -> str:
        filled = int(percent / 10)
        return "🟩" * filled + "⬜" * (10 - filled)
    
    await message.answer(
        f"📊 Твой прогресс за сегодня\n\n"
        f"💧 ВОДА\n"
        f"{progress_bar(water_percent)} {water_percent}%\n"
        f"Выпито: {profile.water_consumed} / {profile.water_target} мл\n"
        f"Осталось: {water_remain} мл\n\n"
        f"🍽 КАЛОРИИ\n"
        f"{progress_bar(calorie_percent)} {calorie_percent}%\n"
        f"Съедено: {profile.calories_eaten} ккал\n"
        f"Сожжено: {profile.calories_burned} ккал\n"
        f"Баланс: {calorie_balance} ккал\n"
        f"Цель: {profile.calorie_target} ккал\n"
        f"Осталось: {calorie_remain} ккал\n\n"
        f"Команды: /charts /tips"
    )


def create_chart(times: list[str], values: list[int], title: str, 
                ylabel: str, target: int | None = None) -> io.BytesIO:
    """Создание графика прогресса"""
    plt.style.use('seaborn-v0_8-darkgrid')
    fig, ax = plt.subplots(figsize=(10, 6))
    
    # Основная линия
    ax.plot(times, values, marker='o', linewidth=2.5, markersize=8,
            color='#2E86AB', label='Фактическое значение')
    
    # Целевая линия
    if target:
        ax.axhline(y=target, color='#A23B72', linestyle='--', 
                  linewidth=2, label=f'Цель: {target}')
        ax.fill_between(range(len(times)), 0, target, alpha=0.1, color='#A23B72')
    
    ax.set_title(title, fontsize=15, fontweight='bold', pad=20)
    ax.set_xlabel('Время', fontsize=12)
    ax.set_ylabel(ylabel, fontsize=12)
    ax.legend(loc='upper left', fontsize=10)
    ax.grid(True, alpha=0.3)
    
    plt.xticks(rotation=45, ha='right')
    plt.tight_layout()
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
    plt.close(fig)
    buf.seek(0)
    
    return buf


@dp.message(Command("charts"))
async def cmd_charts(message: Message):
    profile = get_profile(message.from_user.id)
    if not profile:
        await message.answer(require_profile_message())
        return
    
    if len(profile.water_timeline) < 2 and len(profile.calorie_timeline) < 2:
        await message.answer(
            "📊 Пока недостаточно данных для графиков\n\n"
            "Добавь хотя бы пару записей:\n"
            "/drink 250\n"
            "/eat banana\n"
            "/train бег 20"
        )
        return
    
    # График воды
    if len(profile.water_timeline) >= 2:
        times_w = [t for t, _ in profile.water_timeline]
        values_w = [v for _, v in profile.water_timeline]
        chart_w = create_chart(times_w, values_w, 
                              "📊 Потребление воды за день", 
                              "Миллилитры", profile.water_target)
        
        await message.answer_photo(
            BufferedInputFile(chart_w.getvalue(), filename="water_chart.png"),
            caption="💧 График потребления воды"
        )
    
    # График калорий
    if len(profile.calorie_timeline) >= 2:
        times_c = [t for t, _ in profile.calorie_timeline]
        values_c = [v for _, v in profile.calorie_timeline]
        chart_c = create_chart(times_c, values_c,
                              "📊 Потребление калорий за день",
                              "Килокалории", profile.calorie_target)
        
        await message.answer_photo(
            BufferedInputFile(chart_c.getvalue(), filename="calories_chart.png"),
            caption="🍽 График потребления калорий"
        )
    
    # График сожжённых калорий
    if len(profile.workout_timeline) >= 2:
        times_b = [t for t, _ in profile.workout_timeline]
        values_b = [v for _, v in profile.workout_timeline]
        chart_b = create_chart(times_b, values_b,
                              "📊 Сожжённые калории за день",
                              "Килокалории", None)
        
        await message.answer_photo(
            BufferedInputFile(chart_b.getvalue(), filename="burned_chart.png"),
            caption="🔥 График сожжённых калорий"
        )



@dp.message(Command("tips"))
async def cmd_tips(message: Message):
    profile = get_profile(message.from_user.id)
    if not profile:
        await message.answer(require_profile_message())
        return
    
    recommendations = []
    
    # Анализ воды
    water_deficit = profile.water_target - profile.water_consumed
    if water_deficit > 500:
        portion = min(250, water_deficit)
        recommendations.append(
            f"💧 ВОДА\n"
            f"Осталось выпить {water_deficit} мл\n"
            f"Рекомендую: выпей сейчас {portion} мл, "
            f"затем по {portion} мл каждый час"
        )
    elif water_deficit > 0:
        recommendations.append(f"💧 ВОДА\nПочти достиг цели! Осталось {water_deficit} мл")
    else:
        recommendations.append("💧 ВОДА\n✅ Отлично! Норма выполнена")
    
    # Анализ калорий
    cal_balance = profile.calories_eaten - profile.calories_burned
    cal_deficit = profile.calorie_target - cal_balance
    
    if cal_balance > profile.calorie_target + 300:
        excess = cal_balance - profile.calorie_target
        workout_time = int(excess / (WorkoutCalculator.MET_VALUES.get("ходьба", 4.5) * profile.weight / 60))
        recommendations.append(
            f"🍽 КАЛОРИИ\n"
            f"Превышение: {excess} ккал\n"
            f"Рекомендую: прогулка {workout_time} минут или лёгкая тренировка"
        )
    elif cal_deficit > 300:
        recommendations.append(
            f"🍽 КАЛОРИИ\n"
            f"До цели: {cal_deficit} ккал\n"
            f"Рекомендую: белковый перекус (курица, творог, яйца)"
        )
    elif cal_deficit > 0:
        recommendations.append(f"🍽 КАЛОРИИ\n✅ Почти в цели! Осталось {cal_deficit} ккал")
    else:
        recommendations.append("🍽 КАЛОРИИ\n✅ Цель достигнута!")
    
    # Идеи продуктов
    low_cal_foods = [
        "огурцы (15 ккал/100г)",
        "помидоры (18 ккал/100г)",
        "куриная грудка (110 ккал/100г)",
        "греческий йогурт (60 ккал/100г)",
        "яйца (155 ккал/100г)"
    ]
    recommendations.append(
        f"🥗 НИЗКОКАЛОРИЙНЫЕ ПРОДУКТЫ\n" + "\n".join(f"• {f}" for f in low_cal_foods[:3])
    )
    
    # Идеи активности
    recommendations.append(
        "🏃 ИДЕИ АКТИВНОСТИ\n"
        "• Ходьба 30 мин\n"
        "• Бег 15-20 мин\n"
        "• Велосипед 25 мин\n"
        "• Плавание 20 мин"
    )
    
    await message.answer("\n\n".join(recommendations))


@dp.message(Command("reset"))
async def cmd_reset(message: Message):
    profile = get_profile(message.from_user.id)
    if not profile:
        await message.answer(require_profile_message())
        return
    
    # Сохраняем настройки профиля
    water_target = profile.water_target
    calorie_target = profile.calorie_target
    
    # Сбрасываем метрики
    profile.water_consumed = 0
    profile.calories_eaten = 0
    profile.calories_burned = 0
    profile.water_timeline = [(current_time(), 0)]
    profile.calorie_timeline = [(current_time(), 0)]
    profile.workout_timeline = [(current_time(), 0)]
    
    await message.answer(
        "🔄 Дневные данные сброшены\n\n"
        f"💧 Норма воды: {water_target} мл\n"
        f"🔥 Норма калорий: {calorie_target} ккал\n\n"
        "Начни новый день!\n"
        "/drink /eat /train"
    )


async def main():
    print("🚀 Бот запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
