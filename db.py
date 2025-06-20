import csv
import mysql.connector
import logging
import os
import dotenv
from pathlib import Path

# Загрузка переменных окружения из .env файла
dotenv.load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename='db_import.log',
    filemode='w'
)


def create_connection():
    """Создает подключение к БД с использованием переменных окружения"""
    try:
        return mysql.connector.connect(
            host=os.getenv('DB_HOST', 'localhost'),
            user=os.getenv('DB_USER', 'root'),
            password=os.getenv('DB_PASSWORD', ''),
            database=os.getenv('DB_NAME', 'halal_checker'),
            auth_plugin='mysql_native_password'
        )
    except mysql.connector.Error as err:
        logging.error(f"Ошибка подключения к БД: {err}")
        return None


def import_additives(cnx, csv_path):
    """Импортирует данные из CSV в базу данных"""
    cursor = cnx.cursor(dictionary=True)
    try:
        # Создаем таблицу additives
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS additives (
                id INT AUTO_INCREMENT PRIMARY KEY,
                code VARCHAR(10) NOT NULL UNIQUE,
                name VARCHAR(255) NOT NULL,
                category VARCHAR(100),
                description TEXT,
                status ENUM('halal', 'haram', 'suspicious', 'undefined') NOT NULL DEFAULT 'undefined',
                condition_text TEXT
            )
        """)

        # Пытаемся создать FULLTEXT индекс
        try:
            cursor.execute("""
                CREATE FULLTEXT INDEX ft_search 
                ON additives(name, description)
            """)
            logging.info("Создан полнотекстовый индекс для поиска")
        except mysql.connector.Error as err:
            logging.warning(f"Не удалось создать индекс: {err}")

        # Проверяем существование CSV файла
        if not Path(csv_path).exists():
            raise FileNotFoundError(f"CSV файл не найден: {csv_path}")

        with open(csv_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)

            if not reader.fieldnames:
                raise ValueError("CSV файл пуст или не содержит заголовков")

            logging.info(f"Заголовки CSV: {reader.fieldnames}")

            if 'code' not in reader.fieldnames:
                raise KeyError("Отсутствует обязательная колонка 'code' в CSV")

            total = 0
            imported = 0
            errors = 0

            for row in reader:
                total += 1
                try:
                    # Обработка данных
                    code = row.get('code', '').strip().upper().replace('Е', 'E')
                    if not code:
                        logging.warning(f"Строка {total}: пропущена - отсутствует код")
                        errors += 1
                        continue

                    name = row.get('name', '').strip()
                    category = row.get('category', '').strip()
                    description = row.get('description', '').strip()

                    status = row.get('status', '').strip().lower()
                    if status not in ('halal', 'haram', 'suspicious'):
                        status = 'undefined'

                    condition = row.get('condition', '').strip()

                    # Вставка данных
                    cursor.execute("""
                        INSERT INTO additives (code, name, category, description, status, condition_text)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            name = VALUES(name),
                            category = VALUES(category),
                            description = VALUES(description),
                            status = VALUES(status),
                            condition_text = VALUES(condition_text)
                    """, (code, name, category, description, status, condition))
                    imported += 1

                except Exception as e:
                    logging.error(f"Строка {total}: ошибка - {str(e)}")
                    errors += 1

            cnx.commit()
            logging.info(f"Импорт завершен: Успешно {imported} | Ошибки {errors} | Всего {total}")
            return imported

    except Exception as e:
        logging.critical(f"Критическая ошибка импорта: {e}", exc_info=True)
        if cnx.is_connected():
            cnx.rollback()
        return 0
    finally:
        if cursor:
            cursor.close()


if __name__ == "__main__":
    logging.info("Начало импорта данных")

    # Получаем путь к CSV из аргументов или переменной окружения
    csv_path = os.getenv('CSV_PATH', 'additives.csv')

    cnx = create_connection()
    if cnx and cnx.is_connected():
        try:
            logging.info("Подключение к БД установлено")
            result = import_additives(cnx, csv_path)
            if result > 0:
                logging.info("Импорт выполнен успешно")
            else:
                logging.error("Импорт не выполнен")
        except Exception as e:
            logging.critical(f"Ошибка в основном цикле: {e}", exc_info=True)
        finally:
            cnx.close()
            logging.info("Подключение к БД закрыто")
    else:
        logging.error("Не удалось подключиться к БД")
    logging.info("Завершение работы")