#!/root/halal_bot/venv/bin/python3
import re
import logging
import mysql.connector
import requests
from pathlib import Path
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.enums import ParseMode
from aiogram.types import Message, URLInputFile
from PIL import Image, ImageFilter, ImageOps, ImageEnhance
from pytesseract import TesseractError  # Добавляем в раздел импортов
import pytesseract
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
import html
import asyncio
from tenacity import retry, stop_after_attempt, wait_exponential
from difflib import SequenceMatcher, get_close_matches
import time
import numpy as np
from dotenv import load_dotenv
import os
from pyzbar.pyzbar import decode




# Загрузка переменных из .env
load_dotenv()
# Конфигурация
MYSQL_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', ''),
    'database': os.getenv('DB_NAME', 'halal_checker'),
    'auth_plugin': 'mysql_native_password'
}

TOKEN = os.getenv('BOT_TOKEN')
E_CODE_PATTERN = re.compile(r'\b[E£Е]\d{3}[A-Z]?\b', re.IGNORECASE)  # Учитываем все варианты
ADDITIVES_CACHE = []
USER_STATES = {}

# Настройка логирования
logging.basicConfig(
    level=logging.DEBUG,  # Измените на DEBUG для детальных логов
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot_debug.log"),
        logging.StreamHandler()
    ]
)

# Инициализация бота
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'


# Добавляем модели для локального ИИ
if not os.path.exists('models'):
    os.makedirs('models')


# Загрузка модели для классификации халяль/харам (если доступна)
HALAL_CLASSIFIER = None
try:
    HALAL_CLASSIFIER = pipeline(
        "text-classification",
        model="saved_model" if os.path.exists("saved_model") else "distilbert-base-uncased"
    )
except:
    logging.warning("ИИ-классификатор не загружен. Будет использоваться базовый анализ")







# Инициализация генератора User-Agent
ua = UserAgent()


# Добавляем обработчик команды /start
@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Приветственное сообщение с объяснением работы бота"""
    welcome_text = (
        "🕌 <b>Ассаляму алейкум, дорогие братья и сестры!</b>\n\n"
        "Я - Halal Checker Bot, ваш помощник в определении дозволенности пищевых добавок по стандартам Ислама.\n\n"
        "<b>Как я работаю:</b>\n"
        "1. Вы можете отправить мне <b>фото этикетки</b> продукта 📸\n"
        "2. Или ввести <b>E-код добавки</b> (например, E202) 🔍\n"
        "3. Или написать <b>название добавки</b> (например, сорбат калия)\n\n"
        "Я проанализирую состав и сообщу:\n"
        "✅ <b>Халяль</b> - разрешенные добавки\n"
        "❌ <b>Харам</b> - запрещенные добавки\n"
        "⚠️ <b>Сомнительные</b> - требующие дополнительной проверки\n"
        "❓ <b>Не определено</b> - когда нет достаточной информации\n\n"
        "Используйте команду /help для получения подробной инструкции.\n\n"
        "<i>Пусть Аллах примет ваши благие дела и убережет от запретного!</i>"
    )
    await message.answer(welcome_text)


# Функции для работы со штрих-кодами
def extract_barcode(image_path: str) -> str:
    """Извлекает штрих-код из изображения"""
    try:
        with Image.open(image_path) as img:
            # Предобработка для улучшения распознавания штрих-кода
            img = img.convert('L')
            img = img.point(lambda p: 0 if p < 100 else 255)

            barcodes = decode(img)
            if barcodes:
                return barcodes[0].data.decode("utf-8")
        return ""
    except Exception as e:
        logging.error(f"Ошибка распознавания штрих-кода: {e}")
        return ""


def get_product_info(barcode: str) -> dict:
    """Получает информацию о продукте из нескольких источников"""
    sources = [
        get_product_info_from_barcode_list,
        # get_product_info_from_google,
        # get_product_info_from_another_source  # Можно добавить дополнительные
    ]

    for source in sources:
        try:
            product_info = source(barcode)
            if product_info and product_info.get("name") != "Неизвестный продукт":
                return product_info
        except Exception as e:
            logging.error(f"Ошибка получения информации из источника: {e}")

    return {
        "name": "Неизвестный продукт",
        "brand": "Информация недоступна",
        "image_url": "",
        "source_url": f"https://www.google.com/search?q={barcode}"
    }


def get_product_info_from_barcode_list(barcode: str) -> dict:
    """Получает информацию о продукте с сайта barcode-list.ru"""
    try:
        url = f"https://barcode-list.ru/barcode/RU/barcode-{barcode}/Поиск.htm"
        headers = {
            'User-Agent': ua.random,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.8,en-US;q=0.5,en;q=0.3',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        }

        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')

        # Извлекаем название продукта
        name_element = soup.select_one(
            '#main > div > table > tbody > tr > td.main_column > table > tbody > tr:nth-child(2) > td:nth-child(3)')
        product_name = name_element.text.strip() if name_element else "Неизвестный продукт"

        # Извлекаем бренд
        brand_element = soup.select_one(
            '#main > div > table > tbody > tr > td.main_column > table > tbody > tr:nth-child(3) > td:nth-child(3)')
        brand = brand_element.text.strip() if brand_element else "Неизвестный бренд"

        # Извлекаем изображение
        image_element = soup.select_one(
            '#main > div > table > tbody > tr > td.main_column > table > tbody > tr:nth-child(1) > td > img')
        image_url = image_element['src'] if image_element and 'src' in image_element.attrs else ""

        # Получаем категорию продукта
        category_element = soup.select_one(
            '#main > div > table > tbody > tr > td.main_column > table > tbody > tr:nth-child(4) > td:nth-child(3)')
        category = category_element.text.strip() if category_element else ""

        # Получаем страну производства
        country_element = soup.select_one(
            '#main > div > table > tbody > tr > td.main_column > table > tbody > tr:nth-child(6) > td:nth-child(3)')
        country = country_element.text.strip() if country_element else ""

        return {
            "name": product_name,
            "brand": brand,
            "image_url": image_url,
            "category": category,
            "country": country,
            "source": "barcode-list.ru",
            "source_url": url
        }

    except Exception as e:
        logging.error(f"Ошибка получения информации с barcode-list.ru: {e}")
        return {
            "name": "Неизвестный продукт",
            "brand": "Информация недоступна",
            "image_url": "",
            "source_url": f"https://barcode-list.ru/barcode/RU/barcode-{barcode}/Поиск.htm"
        }
def search_product_composition(product_name: str, barcode: str) -> str:
    """Ищет состав продукта в интернете с улучшенной логикой"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7'
        }

        # Если название продукта неопределенное, используем штрих-код в запросе
        if "неизвестный" in product_name.lower():
            search_query = f"{barcode} состав ингредиенты"
        else:
            search_query = f"{product_name} состав ингредиенты"

        # Кодируем запрос для URL
        encoded_query = requests.utils.quote(search_query)
        search_url = f"https://www.google.com/search?q={encoded_query}"

        response = requests.get(search_url, headers=headers, timeout=20)
        soup = BeautifulSoup(response.text, "html.parser")

        composition = ""

        # Стратегия 1: Поиск в сниппетах Google
        snippet_selectors = [
            ('div', {'class': 'BNeawe s3v9rd AP7Wnd'}),
            ('div', {'class': 'hgKElc'}),
            ('div', {'class': 'LGOjhe'}),
            ('div', {'class': 'kno-rdesc'})
        ]

        for tag, attrs in snippet_selectors:
            elements = soup.find_all(tag, attrs)
            for element in elements:
                text = element.get_text()
                if "состав" in text.lower() or "ингредиенты" in text.lower():
                    composition = text
                    if len(composition) > 100:  # Убедимся, что это не короткий фрагмент
                        return composition

        # Стратегия 2: Поиск в карточках знаний
        knowledge_panel = soup.find('div', class_='kp-blk')
        if knowledge_panel:
            for panel in knowledge_panel.find_all('div', class_='wDYxhc'):
                if "состав" in panel.text.lower():
                    composition = panel.get_text()
                    return composition

        # Стратегия 3: Переход на первый результат
        first_result = soup.find('div', class_='tF2Cxc')
        if first_result:
            link_element = first_result.find('a')
            if link_element and 'href' in link_element.attrs:
                result_url = link_element['href']

                try:
                    # Получаем контент страницы
                    page_response = requests.get(result_url, headers=headers, timeout=25)
                    page_soup = BeautifulSoup(page_response.text, "html.parser")

                    # Поиск состава по ключевым словам
                    keywords = ["состав", "ингредиенты", "ingredients", "компоненты"]

                    # Стратегия 3.1: Поиск по заголовкам
                    for keyword in keywords:
                        heading = page_soup.find(
                            lambda tag: tag.name in ['h2', 'h3', 'h4'] and keyword in tag.text.lower())
                        if heading:
                            next_element = heading.find_next_sibling()
                            if next_element:
                                composition = next_element.get_text()
                                if composition:
                                    return composition

                    # Стратегия 3.2: Поиск в таблицах
                    for keyword in keywords:
                        table = page_soup.find('table', summary=lambda s: s and keyword in s.lower())
                        if not table:
                            table = page_soup.find('table', class_=lambda c: c and keyword in c.lower())

                        if table:
                            composition = table.get_text()
                            return composition

                    # Стратегия 3.3: Поиск в div с классами
                    composition_classes = ["composition", "ingredients", "product-ingredients", "product-composition"]
                    for cls in composition_classes:
                        div = page_soup.find('div', class_=cls)
                        if div:
                            composition = div.get_text()
                            return composition

                    # Стратегия 3.4: Поиск по id
                    composition_ids = ["ingredients", "composition", "sostav"]
                    for id_name in composition_ids:
                        div = page_soup.find('div', id=id_name)
                        if div:
                            composition = div.get_text()
                            return composition

                except Exception as e:
                    logging.error(f"Ошибка при обработке страницы: {e}")

        # Если ничего не найдено, возвращаем сообщение
        return "Состав не найден. Попробуйте поискать вручную."

    except Exception as e:
        logging.error(f"Ошибка поиска состава: {e}")
        return "Не удалось получить состав из-за ошибки"


# async def process_barcode(barcode: str, message: types.Message, image_path: Path):
#     """Обрабатывает штрих-код продукта с улучшенной обработкой ошибок"""
#     try:
#         # Уведомление пользователя
#         await message.answer(f"🔍 Найден штрих-код: <b>{barcode}</b>\nИщу информацию о продукте...")
#
#         # Получение информации о продукте
#         product_info = get_product_info(barcode)
#
#         # Отправка информации о продукте
#         product_text = (
#             f"📦 <b>Продукт:</b> {product_info['name']}\n"
#             f"🏭 <b>Бренд:</b> {product_info['brand']}\n"
#             f"🔗 <a href='{product_info['source_url']}'>Источник</a>"
#         )
#
#         # Добавляем дополнительные поля, если они есть
#         if product_info.get('category'):
#             product_text += f"📋 <b>Категория:</b> {product_info['category']}\n"
#         if product_info.get('country'):
#             product_text += f"🌍 <b>Страна:</b> {product_info['country']}\n"
#
#         product_text += f"🔗 <a href='{product_info['source_url']}'>Источник ({product_info.get('source', 'Google')})</a>"
#
#         # Отправляем изображение, если есть
#         if product_info.get('image_url') and product_info['image_url'].startswith('http'):
#             try:
#                 await message.answer_photo(photo=product_info['image_url'], caption=product_text)
#             except Exception as e:
#                 logging.error(f"Не удалось отправить изображение: {e}")
#                 await message.answer(product_text)
#         else:
#             await message.answer(product_text)
#
#         # Поиск состава продукта
#         await message.answer("🧪 Ищу состав продукта...")
#         composition = search_product_composition(product_info['name'], barcode)
#
#         # Форматируем результат
#         if len(composition) > 1000:
#             composition_display = composition[:500] + "...\n\n... [текст сокращен] ...\n\n" + composition[-500:]
#         else:
#             composition_display = composition
#
#         await message.answer(f"📝 <b>Найденный состав:</b>\n{composition_display}")
#
#         # Анализ состава
#         await message.answer("🔬 Анализирую состав на соответствие халяль...")
#
#         # Здесь должен быть ваш код анализа состава
#         # Временно используем заглушку
#         analysis_result = "✅ Предварительный анализ: продукт, вероятно, халяль"
#
#         # Формирование финального ответа
#         result_text = (
#             f"🕌 <b>Результат проверки:</b>\n{analysis_result}\n\n"
#             f"ℹ️ <b>Рекомендации:</b>\n"
#             f"• Это автоматическая проверка, для важных решений консультируйтесь со специалистами\n"
#             f"• Если состав неполный, попробуйте найти более точную информацию\n"
#             f"• Отправьте /help для дополнительной информации"
#         )
#
#         await message.answer(result_text)
#
#     except Exception as e:
#         logging.error(f"Ошибка обработки штрих-кода: {e}", exc_info=True)
#         await message.answer(
#             "⚠️ Произошла ошибка при обработке штрих-кода.\n\n"
#             "Попробуйте один из этих вариантов:\n"
#             "1. Введите штрих-код вручную (13-14 цифр)\n"
#             "2. Отправьте фото состава продукта\n"
#             "3. Поищите информацию на сайте производителя"
#         )


def analyze_composition(composition: str) -> dict:
    """Анализирует состав продукта с использованием ИИ и базы данных"""

    # 1. Сначала проверяем через базу данных
    found_additives = []
    composition_lower = composition.lower()

    # Поиск E-кодов
    e_codes = re.findall(r'\b[E£Е]\d{3}[A-Z]?\b', composition, re.IGNORECASE)
    normalized_codes = [re.sub(r'[£Е]', 'E', code, flags=re.IGNORECASE) for code in e_codes]
    for code in e_codes:
        details = get_additive_details(code)
        if details:
            found_additives.append(details)

    # Поиск по названиям ингредиентов
    for additive in ADDITIVES_CACHE:
        for term in additive['search_terms']:
            if term and term in composition_lower:
                found_additives.append(additive)
                break

    # 2. Если нашли добавки, возвращаем результат
    if found_additives:
        return {
            "method": "database",
            "additives": found_additives
        }

    # 3. Если не нашли, используем ИИ-классификатор
    if HALAL_CLASSIFIER:
        try:
            result = HALAL_CLASSIFIER(composition[:500])
            label = result[0]['label']
            score = result[0]['score']

            status_map = {
                "LABEL_0": "halal",
                "LABEL_1": "haram",
                "LABEL_2": "suspicious"
            }

            return {
                "method": "ai",
                "status": status_map.get(label, "undefined"),
                "confidence": score,
                "explanation": f"ИИ определил статус как '{status_map.get(label, 'неизвестно')}' с вероятностью {score:.2f}"
            }
        except Exception as e:
            logging.error(f"Ошибка ИИ-классификации: {e}")

    # 4. Если ИИ недоступен, используем базовый анализ по ключевым словам
    haram_keywords = ["свинина", "бекон", "ветчина", "сало", "gelatine", "желатин", "сычужный", "пепсин", "e120",
                      "кошениль", "спирт", "алкоголь"]
    halal_keywords = ["растительный", "пальмовое масло", "подсолнечное масло"]

    haram_found = [kw for kw in haram_keywords if kw in composition_lower]
    halal_found = [kw for kw in halal_keywords if kw in composition_lower]

    if haram_found:
        status = "haram"
        explanation = f"Найдены запрещенные ингредиенты: {', '.join(haram_found)}"
    elif halal_found:
        status = "halal"
        explanation = f"Найдены разрешенные ингредиенты: {', '.join(halal_found)}"
    else:
        status = "suspicious"
        explanation = "Не удалось определить статус автоматически"

    return {
        "method": "keyword",
        "status": status,
        "explanation": explanation
    }


# Обработчики сообщений
@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    """Обновленное приветственное сообщение с новыми функциями"""
    welcome_text = (
        "🕌 <b>Ассаляму алейкум, дорогие братья и сестры!</b>\n\n"
        "Я - Halal Checker Bot, ваш помощник в определении дозволенности продуктов по стандартам Ислама.\n\n"
        "<b>Новые возможности:</b>\n"
        "• <b>Сканирование штрих-кодов</b> - отправьте фото штрих-кода товара\n"
        "• <b>Проверка состава</b> - автоматический поиск информации о продукте\n\n"
        "<b>Как я работаю:</b>\n"
        "1. Отправьте мне <b>фото этикетки</b> продукта 📸\n"
        "2. Или <b>фото штрих-кода</b> товара\n"
        "3. Или введите <b>E-код добавки</b> (например, E202) 🔍\n"
        "4. Или напишите <b>название добавки</b> (например, сорбат калия)\n\n"
        "Я проанализирую состав и сообщу:\n"
        "✅ <b>Халяль</b> - разрешенные продукты\n"
        "❌ <b>Харам</b> - запрещенные продукты\n"
        "⚠️ <b>Сомнительные</b> - требующие дополнительной проверки\n\n"
        "<i>Пусть Аллах примет ваши благие дела и убережет от запретного!</i>"
    )
    await message.answer(welcome_text)


async def process_barcode(barcode: str, message: types.Message, image_path: Path):
    """Обрабатывает штрих-код продукта"""
    try:
        # Уведомление пользователя
        await message.answer(f"🔍 Найден штрих-код: <b>{barcode}</b>\nИщу информацию о продукте в Google...")

        # Получение информации о продукте
        product_info = get_product_info(barcode)

        # Отправка информации о продукте
        product_text = (
            f"📦 <b>Продукт:</b> {product_info['name']}\n"
            f"🏭 <b>Бренд:</b> {product_info['brand']}\n"
            f"🔗 <a href='{product_info['source_url']}'>Источник в Google</a>"
        )

        # Если есть изображение, отправляем его
        if product_info.get('image_url'):
            try:
                await message.answer_photo(
                    photo=product_info['image_url'],
                    caption=product_text
                )
            except:
                await message.answer(product_text)
        else:
            await message.answer(product_text)

        # Добавляем задержку перед следующим запросом
        time.sleep(1)

        # Поиск состава продукта
        await message.answer("🧪 Ищу состав продукта...")
        composition = search_product_composition(product_info['name'], barcode)

        if "не найден" in composition.lower() or "не удалось" in composition.lower():
            await message.answer("❌ Не удалось найти состав продукта. Вы можете ввести его вручную.")
            return

        # Анализ состава
        await message.answer("🔬 Анализирую состав на соответствие халяль...")
        analysis = analyze_composition(composition)

        # Формирование результата
        result_text = f"📝 <b>Состав:</b>\n{composition[:500]}{'...' if len(composition) > 500 else ''}\n\n"

        if analysis["method"] == "database":
            status_names = {
                'halal': '✅ Халяль',
                'haram': '❌ Харам',
                'suspicious': '⚠️ Сомнительное',
                'undefined': '❓ Не определено'
            }

            result_text += "🔍 <b>Найдены добавки:</b>\n"
            for additive in analysis["additives"]:
                status = status_names.get(additive['status'], additive['status'])
                result_text += (
                    f"• <b>{additive['code']}</b> - {additive['name']}\n"
                    f"  Статус: {status}\n"
                    f"  Категория: {additive['category']}\n\n"
                )
        else:
            status_names = {
                'halal': '✅ Халяль',
                'haram': '❌ Харам',
                'suspicious': '⚠️ Сомнительное',
                'undefined': '❓ Не определено'
            }
            status = status_names.get(analysis["status"], "❓ Не определено")

            result_text += (
                f"🕌 <b>Статус:</b> {status}\n"
                f"📋 <b>Объяснение:</b> {analysis.get('explanation', '')}\n"
                f"⚙️ <b>Метод анализа:</b> {'ИИ-классификатор' if analysis['method'] == 'ai' else 'ключевые слова'}"
            )

        result_text += "\n\n⚠️ <i>Это автоматическая проверка. Для важных решений консультируйтесь со специалистами по халяль.</i>"
        await message.answer(result_text)

    except Exception as e:
        logging.error(f"Ошибка обработки штрих-кода: {e}")
        await message.answer("⚠️ Произошла ошибка при обработке штрих-кода. Пожалуйста, попробуйте позже.")


def enhance_image(image_path: Path) -> Image:
    """Улучшение качества изображения для OCR"""
    with Image.open(image_path) as img:
        # Увеличение размера
        new_width = img.width * 3
        new_height = img.height * 3
        img = img.resize((new_width, new_height), Image.LANCZOS)

        # Конвертация в серый цвет
        img = img.convert('L')

        # Автоматическая коррекция контраста
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(3.0)  # Увеличиваем контрастность

        # Увеличение резкости
        enhancer = ImageEnhance.Sharpness(img)
        img = enhancer.enhance(3.0)  # Увеличиваем резкость

        # Удаление шумов
        img = img.filter(ImageFilter.MedianFilter(size=5))

        # Бинаризация с адаптивным порогом
        img = img.point(lambda p: 0 if p < 140 else 255)

        return img


# Улучшенная функция распознавания текста
# def preprocess_image(image_path: Path) -> Image:
#     """Расширенная предобработка изображения для улучшения распознавания"""
#     with Image.open(image_path) as img:
#         # Конвертация в серый цвет
#         img = img.convert('L')
#
#         # Автоматическая коррекция контраста
#         img = ImageOps.autocontrast(img, cutoff=2)
#
#         # Увеличение резкости
#         img = img.filter(ImageFilter.SHARPEN)
#
#         # Удаление шумов
#         img = img.filter(ImageFilter.MedianFilter(size=3))
#
#         # Масштабирование для улучшения разрешения
#         new_width = img.width * 2
#         new_height = img.height * 2
#         img = img.resize((new_width, new_height), Image.LANCZOS)
#
#         # Конвертация в numpy array для обработки
#         img_array = np.array(img)
#
#         # Бинаризация с адаптивным порогом
#         threshold = img_array.mean() * 0.8
#         img = img.point(lambda p: 0 if p < threshold else 255)
#
#         return img


async def extract_text_from_image(image_path: Path) -> str:
    """Извлечение текста с улучшенной обработкой ошибок"""
    temp_path = None  # Инициализируем переменную
    try:
        # Улучшение изображения
        processed_img = enhance_image(image_path)

        # Сохраняем временный файл для обработки
        temp_path = image_path.with_name(f"preprocessed_{image_path.name}")
        processed_img.save(temp_path)

        # Конфигурация Tesseract
        custom_config = r'--oem 3 --psm 6 -l rus+eng'

        # Распознавание текста
        text = pytesseract.image_to_string(str(temp_path), config=custom_config)

        # Очистка текста
        return clean_ocr_text(text)

    except Exception as e:
        logging.error(f"Ошибка OCR: {str(e)}")
        return ""
    finally:
        # Удаление временного файла
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception as e:
                logging.error(f"Ошибка удаления временного файла: {e}")

def similar(a: str, b: str) -> float:
    """Вычисляет коэффициент сходства между двумя строками"""
    return SequenceMatcher(None, a, b).ratio()


# Улучшенная функция обработки этикетки
async def process_label(image_path: Path, message: types.Message):
    """Обработка фото этикетки с улучшенным распознаванием"""
    try:
        # Извлекаем текст
        text = await extract_text_from_image(image_path)
        if not text:
            await message.answer("❌ Не удалось распознать текст. Попробуйте другое фото.")
            return

        # Отправляем распознанный текст (сокращенный)
        clean_text = text.replace('|', 'I').replace('[', 'I').replace(']', 'I')
        clean_text = re.sub(r'\s+', ' ', clean_text)
        await message.answer(f"🔍 Распознанный текст:\n{html.escape(clean_text[:3000])}" +
                             ("..." if len(clean_text) > 3000 else ""))

        # Анализируем текст
        found_codes = analyze_text(text)
        if not found_codes:
            await message.answer("🔍 Не найдено пищевых добавок в составе")
            return

        # Получаем информацию о добавках
        results = []
        unique_codes = set()
        for code in found_codes:
            if code in unique_codes:
                continue

            unique_codes.add(code)
            details = await get_additive_details(code)
            if details:
                status_names = {
                    'halal': '✅ Халяль',
                    'haram': '❌ Харам',
                    'suspicious': '⚠️ Сомнительное',
                    'undefined': '❓ Не определено'
                }
                status = status_names.get(details['status'], details['status'])
                results.append(
                    f"• <b>{details['code']}</b> - {details['name']}\n"
                    f"  Статус: {status}\n"
                    f"  Категория: {details['category']}\n"
                )

        # Формирование ответа
        response_parts = [
            "🔍 <b>Найденные добавки в составе:</b>\n" + "\n".join(results),
            "\n⚠️ <b>Внимание!</b> Это не все добавки в продукте!",
            "Если вы видите другие E-коды или ингредиенты, проверьте их:",
            "1. Введите E-код: <code>/e КОД</code> (например <code>/e E621</code>)",
            "2. Или отправьте название добавки"
        ]

        await message.answer("\n".join(response_parts))

    except Exception as e:
        logging.error(f"Ошибка обработки этикетки: {e}")
        await message.answer("⚠️ Ошибка обработки этикетки. Попробуйте другое фото.")
    finally:
        if image_path.exists():
            image_path.unlink()


# Добавляем команду для ручной проверки E-кода
@dp.message(Command("e"))
async def check_e_code(message: Message):
    """Проверяет конкретный E-код"""
    args = message.text.split()
    if len(args) < 2:
        await message.answer("ℹ️ Пожалуйста, укажите E-код после команды /e\nПример: <code>/e E621</code>")
        return

    e_code = args[1].upper().replace('Е', 'E').replace('£', 'E')

    # Нормализация кода
    e_code = re.sub(r'[^E0-9]', '', e_code)
    if not re.match(r'^E\d{3,4}$', e_code):
        await message.answer("❌ Неверный формат E-кода. Используйте формат: EXXX")
        return

    details = await get_additive_details(e_code)
    if not details:
        await message.answer(f"❌ Добавка {e_code} не найдена в базе данных")
        return

    status_names = {
        'halal': '✅ Халяль',
        'haram': '❌ Харам',
        'suspicious': '⚠️ Сомнительное',
        'undefined': '❓ Не определено'
    }
    status = status_names.get(details['status'], details['status'])

    response = (
        f"🔍 <b>{details['code']} - {details['name']}</b>\n"
        f"📦 <b>Категория:</b> {details['category'] or 'Не указана'}\n"
        f"🕌 <b>Статус:</b> {status}\n\n"
    )

    if details.get('description'):
        response += f"📝 <b>Описание:</b>\n{details['description'][:300]}\n\n"

    if details.get('condition_text'):
        response += f"ℹ️ <b>Условие:</b> {details['condition_text']}"

    response += "\n\n⚠️ Это автоматическая проверка. Для важных решений консультируйтесь со специалистами"

    await message.answer(response)
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
async def download_file_with_retry(file, destination):
    return await bot.download(file, destination)


def clean_ocr_text(text: str) -> str:
    """Очистка текста после OCR с учетом русской 'е'"""
    # Заменяем часто неправильно распознаваемые символы
    replacements = {
        'E ': 'E',
        'Е ': 'E',
        'е ': 'E',  # русская е
        '£': 'E',
        ' ': ' ',
    }

    for wrong, correct in replacements.items():
        text = text.replace(wrong, correct)

    # Заменяем русскую "е" в кодах (например "е322")
    text = re.sub(r'\b([еЕ])(\d{3})', r'E\2', text)

    # Удаляем лишние пробелы в E-кодах
    text = re.sub(r'\b([EЕ])\s?(\d{3})\b', r'\1\2', text)

    # Удаляем специальные символы, кроме нужных
    text = re.sub(r'[^\w\s,;.:()E\d-]', '', text)

    # Заменяем множественные пробелы
    text = re.sub(r'\s+', ' ', text).strip()

    return text

# Категории добавок для поиска
ADDITIVE_CATEGORIES = [
    "красители", "консерванты", "антиокислители", "стабилизаторы",
    "эмульгаторы", "усилители вкуса", "пеногасители"
]

def normalize_e_code(code: str) -> str:
    """Нормализация E-кода: приведение к стандартному виду EXXX"""
    code = code.upper().replace('Е', 'E').replace('£', 'E')
    code = re.sub(r'[^E0-9]', '', code)
    if not code.startswith('E'):
        code = 'E' + code.lstrip('0')
    return code


def find_e_codes_in_text(text: str) -> list:
    """Находит все E-коды в тексте с улучшенной обработкой"""
    # Основной поиск
    pattern = r'\b[EЕ£][\s\-]?\d{2,4}[a-zA-Z]?\b'
    matches = re.findall(pattern, text)

    # Дополнительный поиск в сложных случаях
    if not matches:
        matches = re.findall(r'\b[EЕ£][\s\-]?[бБзЗОоА-Яа-я\d]{3,4}\b', text)

    # Нормализация найденных кодов
    normalized_codes = []
    for code in matches:
        try:
            clean_code = re.sub(r'[^E0-9]', '', code)
            clean_code = clean_code.replace('О', '0').replace('о', '0')
            clean_code = clean_code.replace('б', '6').replace('Б', '6')
            clean_code = clean_code.replace('з', '3').replace('З', '3')

            # Форматирование в правильный вид
            if re.match(r'^E\d{3,4}$', clean_code):
                normalized_codes.append(clean_code.upper())
        except:
            continue

    return list(set(normalized_codes))


def find_additives_by_category(text: str) -> list:
    """Ищет добавки по категориям в тексте"""
    found_additives = []
    text_lower = text.lower()

    for category in ADDITIVE_CATEGORIES:
        if category in text_lower:
            # Ищем категорию в тексте
            start_idx = text_lower.find(category)
            if start_idx != -1:
                # Берем контекст вокруг категории (200 символов)
                start = max(0, start_idx - 100)
                end = min(len(text), start_idx + len(category) + 200)
                context = text[start:end]

                # Ищем E-коды в этом контексте
                e_codes = find_e_codes_in_text(context)
                found_additives.extend(e_codes)

    return found_additives


def find_additives_by_name(text: str, threshold: float = 0.8) -> list:
    """Нечеткий поиск добавок по названию"""
    found_codes = set()
    text_lower = text.lower()

    # Собираем все возможные названия добавок
    all_names = []
    for additive in ADDITIVES_CACHE:
        all_names.append(additive['name'].lower())
        if additive.get('search_terms'):
            all_names.extend([t.lower() for t in additive['search_terms']])

    # Ищем близкие совпадения
    words = re.findall(r'\b\w{4,}\b', text_lower)  # Слова из 4+ букв
    for word in words:
        # Пропускаем E-коды
        if re.match(r'^e\d+$', word):
            continue

        # Ищем похожие названия
        matches = get_close_matches(word, all_names, n=1, cutoff=threshold)
        if matches:
            match_name = matches[0]
            # Находим код добавки по названию
            for additive in ADDITIVES_CACHE:
                if (additive['name'].lower() == match_name or
                        (additive.get('search_terms') and match_name in [t.lower() for t in additive['search_terms']])):
                    found_codes.add(additive['code'])
                    break

    return list(found_codes)


def analyze_text(text: str) -> list:
    """Многоуровневый анализ текста на наличие добавок"""
    # 1. Поиск E-кодов
    found_codes = find_e_codes_in_text(text)

    # 2. Поиск по категориям
    found_codes.extend(find_additives_by_category(text))

    # 3. Нечеткий поиск по названиям
    found_codes.extend(find_additives_by_name(text))

    # 4. Удаление дубликатов и нормализация
    unique_codes = set()
    for code in found_codes:
        try:
            norm_code = normalize_e_code(code)
            if re.match(r'^E\d{3,4}$', norm_code):
                unique_codes.add(norm_code)
        except:
            continue

    return list(unique_codes)




# Улучшенная функция кэширования добавок
async def cache_additives():
    """Кэширование добавок с расширенными поисковыми терминами"""
    global ADDITIVES_CACHE
    conn = None
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)

        with conn.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT code, name, category, description, status, condition_text FROM additives")

            ADDITIVES_CACHE.clear()
            for row in cursor:
                search_terms = set()

                # Основное название
                russian_name = row['name'].strip().lower()
                search_terms.add(russian_name)

                # Альтернативные названия из описания
                if row.get('description'):
                    desc = row['description'].lower()
                    # Извлекаем названия в скобках
                    synonyms = re.findall(r'\((.*?)\)', desc)
                    for synonym in synonyms:
                        for syn in re.split(r'[,;]', synonym):
                            clean_syn = syn.strip()
                            if len(clean_syn) > 3:
                                search_terms.add(clean_syn)

                # Ключевые слова из категории
                if row.get('category'):
                    category = row['category'].lower()
                    search_terms.add(category)

                # Добавляем добавку в кэш
                additive_data = {
                    'code': row['code'].upper(),
                    'name': row['name'],
                    'category': row['category'],
                    'status': row['status'],
                    'description': row['description'],
                    'condition_text': row['condition_text'],
                    'search_terms': list(search_terms)
                }
                ADDITIVES_CACHE.append(additive_data)

        logging.info(f"Кэшировано {len(ADDITIVES_CACHE)} добавок")

    except Exception as e:
        logging.error(f"Ошибка кэширования: {e}", exc_info=True)
    finally:
        if conn and conn.is_connected():
            conn.close()


async def get_additive_details(search_term: str) -> dict:
    """Поиск добавки в кэше"""
    # Нормализация E-кода
    if re.match(r'^[eеEЕ]\d{3,4}$', search_term, re.IGNORECASE):
        search_term = normalize_e_code(search_term)

    # Поиск по коду
    for additive in ADDITIVES_CACHE:
        if additive['code'] == search_term.upper():
            return additive

    # Поиск по названию и синонимам
    search_lower = search_term.lower()
    for additive in ADDITIVES_CACHE:
        if search_lower == additive['name'].lower():
            return additive

        if search_lower in [term.lower() for term in additive.get('search_terms', [])]:
            return additive

    # Нечеткий поиск
    best_match = None
    best_score = 0.0
    for additive in ADDITIVES_CACHE:
        # Сравнение с названием
        name_score = SequenceMatcher(None, search_lower, additive['name'].lower()).ratio()
        if name_score > best_score:
            best_score = name_score
            best_match = additive

        # Сравнение с поисковыми терминами
        for term in additive.get('search_terms', []):
            term_score = SequenceMatcher(None, search_lower, term.lower()).ratio()
            if term_score > best_score:
                best_score = term_score
                best_match = additive

    # Порог схожести 75%
    if best_score > 0.75:
        return best_match

    return None


# Исправленная функция поиска в БД
async def get_additive_details(search_term: str) -> dict:
    logging.info(f"Поиск добавки: {search_term}")
    conn = None
    cursor = None
    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
        cursor = conn.cursor(dictionary=True, buffered=True)

        # Поиск по точному совпадению кода
        cursor.execute("SELECT * FROM additives WHERE code = %s", (search_term,))
        result = cursor.fetchone()
        if result:
            logging.info(f"Найдено по коду: {search_term}")
            return result

        # Поиск по названию (точное совпадение)
        cursor.execute("SELECT * FROM additives WHERE LOWER(name) = %s", (search_term.lower(),))
        result = cursor.fetchone()
        if result:
            logging.info(f"Найдено по точному названию: {search_term}")
            return result

        # Поиск по частичному совпадению в названии
        cursor.execute("SELECT * FROM additives WHERE LOWER(name) LIKE %s", (f"%{search_term.lower()}%",))
        result = cursor.fetchone()
        if result:
            logging.info(f"Найдено по частичному названию: {search_term}")
            return result

        logging.warning(f"Не найдено: {search_term}")
        return None

    except mysql.connector.Error as err:
        logging.error(f"Ошибка MySQL: {err}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📋 <b>Halal Checker Bot - Инструкция</b>\n\n"
        "🖼️ <b>1. Анализ фото этикетки</b>\n"
        "Просто отправьте фото состава продукта. Я автоматически распознаю текст и проверю все пищевые добавки!\n\n"
        "🔢 <b>2. Поиск по E-коду</b>\n"
        "Введите код добавки: <code>E202</code> или <code>Е415</code> (можно как латиницей, так и кириллицей)\n\n"
        "📝 <b>3. Поиск по названию</b>\n"
        "Напишите название добавки: <code>сорбат калия</code> или <code>глутамат натрия</code>\n\n"
        "🔄 <b>4. Поиск по описанию</b>\n"
        "Можно искать по словам из описания: <code>краситель</code> или <code>эмульгатор</code>\n\n"
        "📊 <b>Система статусов:</b>\n"
        "✅ <b>Халяль</b> - разрешенные добавки\n"
        "❌ <b>Харам</b> - запрещенные добавки\n"
        "⚠️ <b>Сомнительное</b> - требует дополнительной проверки\n"
        "❓ <b>Не определено</b> - недостаточно информации\n\n"
        "💡 <b>Важно!</b> Бот автоматически распознает как латинские <code>E</code>, так и кириллические <code>Е</code> в кодах добавок\n\n"
        "📬 <b>Обратная связь:</b>\n"
        "Если у вас есть вопросы или пожелания, обращайтесь к @ваш_админ"
    )
    await message.answer(help_text)


@dp.message(lambda message: message.photo)
async def handle_photo(message: types.Message):
    """Обработчик фото с улучшенной логикой"""
    temp_dir = Path("temp_images")
    temp_dir.mkdir(exist_ok=True)

    try:
        # Скачивание фото
        file_id = message.photo[-1].file_id
        file = await bot.get_file(file_id)
        dest_path = temp_dir / f"{file_id}.jpg"

        await bot.download(file, dest_path)

        # Попытка распознать штрих-код
        barcode = extract_barcode(dest_path)

        if barcode:
            await message.answer(f"📦 Найден штрих-код: {barcode}")
            await process_barcode(barcode, message, dest_path)
        else:
            await message.answer("🔍 Анализирую состав продукта...")
            await process_label(dest_path, message)

    except Exception as e:
        logging.error(f"Ошибка обработки фото: {str(e)}")
        await message.answer("⚠️ Не удалось обработать изображение. Попробуйте другое фото.")

    finally:
        # Отложенное удаление файлов
        await asyncio.sleep(10)
        if dest_path.exists():
            try:
                dest_path.unlink()
            except:
                pass


@dp.message(Command("barcode"))
async def handle_barcode_command(message: Message):
    """Обработчик команды /barcode"""
    USER_STATES[message.from_user.id] = {'state': 'waiting_barcode_photo'}
    await message.answer("📷 Пожалуйста, отправьте фото штрих-кода продукта")


@dp.message(F.photo, lambda message: USER_STATES.get(message.from_user.id, {}).get('state') == 'waiting_barcode_photo')
async def handle_barcode_photo(message: types.Message):
    """Обработка фото штрих-кода"""
    user_id = message.from_user.id
    temp_dir = Path("temp")
    temp_dir.mkdir(exist_ok=True)
    file_id = message.photo[-1].file_id
    file = await bot.get_file(file_id)
    destination = temp_dir / f"{file_id}.jpg"

    try:
        await bot.download(file=file, destination=destination)
        barcode = extract_barcode(destination)

        if barcode:
            # Получаем информацию о продукте
            product_info = get_product_info(barcode)
            USER_STATES[user_id] = {
                'state': 'waiting_composition_photo',
                'product_name': product_info['name'],
                'barcode': barcode
            }

            # Формируем запрос к @pic
            search_query = f"{product_info['name']} состав ингредиенты"
            await message.answer(
                f"🔍 Найден продукт: <b>{product_info['name']}</b>\n"
                f"📸 Пожалуйста, используйте бота @pic для поиска фото состава:\n"
                f"<code>@pic {search_query}</code>\n\n"
                "После получения результатов выберите лучшее фото состава и отправьте его мне."
            )
        else:
            await message.answer("❌ Не удалось распознать штрих-код. Попробуйте другое фото.")

    except Exception as e:
        logging.error(f"Ошибка обработки штрих-кода: {e}")
        await message.answer("⚠️ Ошибка обработки фото. Попробуйте еще раз.")
    finally:
        if destination.exists():
            destination.unlink()


@dp.message(F.photo,
            lambda message: USER_STATES.get(message.from_user.id, {}).get('state') == 'waiting_composition_photo')
async def handle_composition_photo(message: types.Message):
    """Обработка фото состава после поиска по баркоду"""
    user_id = message.from_user.id
    user_state = USER_STATES.get(user_id, {})

    if not user_state or user_state.get('state') != 'waiting_composition_photo':
        return

    temp_dir = Path("temp")
    temp_dir.mkdir(exist_ok=True)
    file_id = message.photo[-1].file_id
    file = await bot.get_file(file_id)
    destination = temp_dir / f"{file_id}.jpg"

    try:
        await bot.download(file=file, destination=destination)

        # Извлекаем текст из изображения
        text = await extract_text_from_image(destination)
        if not text:
            await message.answer("❌ Не удалось распознать текст на фото. Попробуйте другое изображение.")
            return

        # Анализируем текст
        found_codes = analyze_text(text)
        if not found_codes:
            await message.answer("🔍 Не найдено пищевых добавок в составе")
            return

        # Получаем информацию о добавках
        additives_info = []
        for code in found_codes:
            details = await get_additive_details(code)
            if details:
                status_names = {
                    'halal': '✅ Халяль',
                    'haram': '❌ Харам',
                    'suspicious': '⚠️ Сомнительное',
                    'undefined': '❓ Не определено'
                }
                status = status_names.get(details['status'], details['status'])

                additives_info.append(
                    f"• <b>{details['code']}</b> - {details['name']}\n"
                    f"  Статус: {status}\n"
                    f"  Категория: {details['category']}\n"
                )

        # Формируем ответ
        response = (
                f"📝 <b>Состав продукта {user_state['product_name']}:</b>\n"
                f"{text[:500]}{'...' if len(text) > 500 else ''}\n\n"
                "🔍 <b>Найденные добавки:</b>\n" +
                "\n".join(additives_info) +
                "\n⚠️ Это не все добавки в продукте! Для полной проверки изучите состав внимательно."
        )

        await message.answer(response)
        del USER_STATES[user_id]

    except Exception as e:
        logging.error(f"Ошибка анализа состава: {e}")
        await message.answer("⚠️ Ошибка обработки состава. Попробуйте еще раз.")
    finally:
        if destination.exists():
            destination.unlink()

@dp.message(F.text)
async def handle_text(message: Message):
    """Обработка текстовых сообщений только с текстом"""
    text = message.text.strip()

    if not text:
        return await message.answer("Пожалуйста, введите код или название пищевой добавки")

    # Поиск в кэше с учетом частичных совпадений
    found_in_cache = []
    for additive in ADDITIVES_CACHE:
        # Проверяем код
        if text.lower() == additive['code'].lower():
            found_in_cache.append(additive)
            continue

        # Проверяем название и синонимы
        for term in additive.get('search_terms', []):
            if text.lower() in term.lower() or term.lower() in text.lower():
                found_in_cache.append(additive)
                break

    if found_in_cache:
        # Берем первую найденную добавку
        details = found_in_cache[0]
    else:
        # Поиск в БД с использованием LIKE для частичного совпадения
        details = await get_additive_details(text)

    if not details:
        # Попробуем найти похожие варианты
        suggestions = []
        for additive in ADDITIVES_CACHE:
            if SequenceMatcher(None, text.lower(), additive['name'].lower()).ratio() > 0.6:
                suggestions.append(additive['name'])

        if suggestions:
            unique_suggestions = list(set(suggestions))[:5]
            reply = "❌ Точного совпадения. Возможно вы искали:\n" + "\n".join(f"• {s}" for s in unique_suggestions)
            return await message.answer(reply)

        return await message.answer("❌ Добавка не найдена. Проверьте написание или уточните запрос.")

    # Форматирование ответа
    status_names = {
        'halal': '✅ Халяль',
        'haram': '❌ Харам',
        'suspicious': '⚠️ Сомнительное',
        'undefined': '❓ Не определено'
    }
    status = status_names.get(details['status'], details['status'])

    response = (
        f"🔍 <b>{details['code']} - {details['name']}</b>\n"
        f"📦 <b>Категория:</b> {details['category'] or 'Не указана'}\n"
        f"🕌 <b>Статус:</b> {status}\n\n"
    )

    if details.get('description'):
        clean_desc = details['description'].replace('"', '').strip()
        response += f"📝 <b>Описание:</b>\n{clean_desc[:250]}{'...' if len(clean_desc) > 250 else ''}\n\n"

    if details.get('condition_text'):
        response += f"ℹ️ <b>Условие:</b> {details['condition_text']}"

    await message.answer(response)

async def main():
    await cache_additives()
    await dp.start_polling(bot)

if __name__ == '__main__':
    asyncio.run(main())
