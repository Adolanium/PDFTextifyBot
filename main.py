import telebot
import fitz
import pytesseract
from PIL import Image
import os
from datetime import datetime
import json
from telebot import types
from concurrent.futures import ProcessPoolExecutor, as_completed
import io
import traceback
import time
import requests
import shutil

try:
    with open('config.json') as config_file:
        config = json.load(config_file)
except FileNotFoundError:
    print("Error: config.json not found. Please create it with your bot token and optionally tesseract_path.")
    exit()
except json.JSONDecodeError:
    print("Error: config.json is not valid JSON.")
    exit()

if 'token' not in config:
    print("Error: 'token' not found in config.json.")
    exit()

EFFECTIVE_TESSERACT_CMD = None
try:
    if 'tesseract_path' in config and config['tesseract_path'] and os.path.exists(config['tesseract_path']):
        pytesseract.pytesseract.tesseract_cmd = config['tesseract_path']
        EFFECTIVE_TESSERACT_CMD = config['tesseract_path']
        print(f"Tesseract path set from config.json: {EFFECTIVE_TESSERACT_CMD}")
    else:
        version = pytesseract.get_tesseract_version()
        tesseract_exe_path = shutil.which('tesseract')
        if tesseract_exe_path:
            pytesseract.pytesseract.tesseract_cmd = tesseract_exe_path
            EFFECTIVE_TESSERACT_CMD = tesseract_exe_path
            print(f"Tesseract found in PATH and configured: {EFFECTIVE_TESSERACT_CMD} (Version: {version})")
        else:
            EFFECTIVE_TESSERACT_CMD = pytesseract.pytesseract.tesseract_cmd
            print(f"Warning: Tesseract executable path not explicitly found by shutil.which(). "
                  f"Relying on pytesseract's default command: '{EFFECTIVE_TESSERACT_CMD}'. Version: {version}. "
                  f"Ensure this command is accessible to worker processes.")

except pytesseract.TesseractNotFoundError:
    print("ERROR: Tesseract executable not found or not configured correctly in config.json or system PATH. OCR will likely fail.")
except Exception as e:
     print(f"Warning: An error occurred during Tesseract setup: {e}")
     traceback.print_exc()

bot = telebot.TeleBot(config['token'])
BASE_DIR = "bot_storage"
USERS_DIR = os.path.join(BASE_DIR, "users")
SUPPORTED_LANGUAGES = {'עברית': 'heb', 'אנגלית': 'eng', 'רוסית': 'rus'}
processing_files = {}

for directory in [BASE_DIR, USERS_DIR]:
    if not os.path.exists(directory):
        os.makedirs(directory)

MAX_OCR_WORKERS = os.cpu_count()
if MAX_OCR_WORKERS is None or MAX_OCR_WORKERS < 1:
    MAX_OCR_WORKERS = 1
print(f"Using up to {MAX_OCR_WORKERS} OCR worker processes.")


def log_user_action(user_id, username, action):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] User ID: {user_id}, Username: {username}, Action: {action}")

def get_user_directories(user_id):
    user_base_dir = os.path.join(USERS_DIR, str(user_id))
    user_pdfs_dir = os.path.join(user_base_dir, "pdfs")
    user_results_dir = os.path.join(user_base_dir, "results")
    for directory in [user_base_dir, user_pdfs_dir, user_results_dir]:
        if not os.path.exists(directory):
            os.makedirs(directory)
    return user_pdfs_dir, user_results_dir

def rotate_image(img, angle):
    if angle == 0:
        return img
    return img.rotate(angle, expand=True)

def enhance_image_for_ocr(image):
    if image.mode != 'L':
        image = image.convert('L')
    image = image.point(lambda x: 0 if x < 128 else 255, '1')
    return image

def process_page_ocr(page_num, image_bytes, language, rotation_angle, tesseract_cmd_for_worker=None):
    try:
        if tesseract_cmd_for_worker:
             pytesseract.pytesseract.tesseract_cmd = tesseract_cmd_for_worker

        img = Image.open(io.BytesIO(image_bytes))
        rotated_img = rotate_image(img, rotation_angle)
        enhanced_img = enhance_image_for_ocr(rotated_img)
        custom_config = f'--oem 3 --psm 6 -l {language}'
        text = pytesseract.image_to_string(enhanced_img, lang=language, config=custom_config)
        return page_num + 1, text.strip()
    except Exception as e:
        tb_str = traceback.format_exc()
        print(f"[Worker Error] Page {page_num + 1}: Failed OCR processing - {e}\n{tb_str}")
        return page_num + 1, f"--- Error processing page {page_num + 1} ---"

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    log_user_action(user_id, username, "Started the bot")
    get_user_directories(user_id)
    try:
        bot.reply_to(message, "שלום! שלח/י לי קובץ PDF ואחלץ ממנו את הטקסט.")
    except Exception as e:
        log_user_action(user_id, username, f"Error sending welcome: {e}")

@bot.message_handler(commands=['myid'])
def get_user_id(message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    log_user_action(user_id, username, "Requested their ID")
    try:
        bot.reply_to(message, f"Your Telegram ID is: {user_id}")
    except Exception as e:
        log_user_action(user_id, username, f"Error sending ID: {e}")


@bot.message_handler(content_types=['document'])
def handle_pdf(message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    try:
        if message.document.mime_type != 'application/pdf':
            log_user_action(user_id, username, f"Sent non-PDF file: {message.document.file_name}")
            bot.reply_to(message, "אנא שלח/י קובץ PDF.")
            return

        log_user_action(user_id, username, f"Uploaded PDF: {message.document.file_name}")
        user_pdfs_dir, user_results_dir = get_user_directories(user_id)

        file_info = bot.get_file(message.document.file_id)
        if file_info.file_size > 20 * 1024 * 1024:
            log_user_action(user_id, username, f"PDF too large: {message.document.file_name} ({file_info.file_size} bytes)")
            bot.reply_to(message, "הקובץ PDF גדול מדי (מעל 20MB). אנא שלח/י קובץ קטן יותר.")
            return

        original_filename = message.document.file_name
        original_name = os.path.splitext(original_filename)[0]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_original_filename = os.path.basename(original_filename)
        pdf_filename = f"{timestamp}_{safe_original_filename}"
        pdf_path = os.path.join(user_pdfs_dir, pdf_filename)

        downloaded_file = bot.download_file(file_info.file_path)
        with open(pdf_path, 'wb') as new_file:
            new_file.write(downloaded_file)

        processing_files[user_id] = {
            'pdf_path': pdf_path,
            'original_name': original_name,
            'pdf_filename': pdf_filename,
            'results_dir': user_results_dir
        }
        ask_language(message)

    except telebot.apihelper.ApiTelegramException as te:
        log_user_action(user_id, username, f"Telegram API error handling PDF: {str(te)}")
        bot.reply_to(message, f"אירעה שגיאת API בקליטת הקובץ: {str(te)}. ייתכן שהקובץ גדול מדי.")
        if user_id in processing_files:
            del processing_files[user_id]
    except Exception as e:
        log_user_action(user_id, username, f"Error handling PDF: {str(e)}\n{traceback.format_exc()}")
        bot.reply_to(message, f"אירעה שגיאה בקליטת הקובץ: {str(e)}")
        if user_id in processing_files:
            del processing_files[user_id]

def ask_language(message):
    markup = types.ReplyKeyboardMarkup(row_width=1, one_time_keyboard=True, resize_keyboard=True)
    language_buttons = [types.KeyboardButton(lang_name) for lang_name in SUPPORTED_LANGUAGES.keys()]
    markup.add(*language_buttons)
    try:
        bot.send_message(message.chat.id, "באיזו שפה כתוב המסמך?", reply_markup=markup)
    except Exception as e:
        log_user_action(message.from_user.id, message.from_user.username or "Unknown", f"Error asking language: {e}")

@bot.message_handler(func=lambda message: message.text in SUPPORTED_LANGUAGES.keys())
def handle_language_selection(message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    try:
        if user_id not in processing_files:
            bot.reply_to(message, "אנא שלח/י קודם קובץ PDF.", reply_markup=types.ReplyKeyboardRemove())
            return

        selected_language_name = message.text
        selected_language_code = SUPPORTED_LANGUAGES[selected_language_name]
        log_user_action(user_id, username, f"Selected language: {selected_language_name} ({selected_language_code})")

        processing_files[user_id]['language'] = selected_language_code
        send_first_page_preview(message, user_id)

    except Exception as e:
        log_user_action(user_id, username, f"Error handling language selection: {str(e)}\n{traceback.format_exc()}")
        bot.reply_to(message, f"אירעה שגיאה בבחירת השפה: {str(e)}", reply_markup=types.ReplyKeyboardRemove())
        if user_id in processing_files:
            del processing_files[user_id]

def send_first_page_preview(message, user_id):
    if user_id not in processing_files: return

    file_data = processing_files[user_id]
    pdf_path = file_data['pdf_path']
    username = message.from_user.username or "Unknown"

    temp_preview_dir = os.path.join(BASE_DIR, "temp_previews")
    if not os.path.exists(temp_preview_dir):
        os.makedirs(temp_preview_dir)
    
    preview_filename = f"preview_{user_id}_{datetime.now().strftime('%Y%m%d%H%M%S%f')}.jpg"
    preview_path = os.path.join(temp_preview_dir, preview_filename)

    try:
        doc = fitz.open(pdf_path)
        if doc.page_count == 0:
            log_user_action(user_id, username, "PDF has no pages, cannot generate preview.")
            bot.reply_to(message, "הקובץ PDF ריק או פגום, לא ניתן ליצור תצוגה מקדימה.", reply_markup=types.ReplyKeyboardRemove())
            if user_id in processing_files: del processing_files[user_id]
            if doc: doc.close()
            return

        page = doc.load_page(0)
        pix = page.get_pixmap(dpi=150)
        doc.close()

        pix.save(preview_path, "jpeg")

        with open(preview_path, 'rb') as preview_file:
            bot.send_photo(
                message.chat.id,
                preview_file,
                caption="הנה תצוגה מקדימה של העמוד הראשון. אנא בחר/י את כיוון הדף הנכון:",
            )
        ask_rotation(message)

    except Exception as e:
        log_user_action(user_id, username, f"Error generating preview: {str(e)}\n{traceback.format_exc()}")
        bot.reply_to(message, f"אירעה שגיאה בהכנת תצוגה מקדימה: {str(e)}", reply_markup=types.ReplyKeyboardRemove())
        if user_id in processing_files: del processing_files[user_id]
    finally:
        if os.path.exists(preview_path):
            try:
                os.remove(preview_path)
            except OSError as oe:
                 log_user_action(user_id, username, f"Warning: Could not delete temp preview file {preview_path}: {oe}")

def ask_rotation(message):
    markup = types.ReplyKeyboardMarkup(row_width=2, one_time_keyboard=True, resize_keyboard=True)
    markup.add(
        types.KeyboardButton("0° (רגיל)"), types.KeyboardButton("90° (ימינה)"),
        types.KeyboardButton("180° (הפוך)"), types.KeyboardButton("270° (שמאלה)")
    )
    try:
        bot.send_message(message.chat.id, "באיזו זווית יש לסובב את כל העמודים?", reply_markup=markup)
    except Exception as e:
        log_user_action(message.from_user.id, message.from_user.username or "Unknown", f"Error asking rotation: {e}")

@bot.message_handler(func=lambda message: message.text in ["0° (רגיל)", "90° (ימינה)", "180° (הפוך)", "270° (שמאלה)"])
def handle_rotation_selection(message):
    user_id = message.from_user.id
    username = message.from_user.username or "Unknown"
    try:
        if user_id not in processing_files:
            bot.reply_to(message, "אנא שלח/י קודם קובץ PDF.", reply_markup=types.ReplyKeyboardRemove())
            return

        if 'language' not in processing_files[user_id]:
             bot.reply_to(message, "אנא בחר/י שפה תחילה.", reply_markup=types.ReplyKeyboardRemove())
             return

        angle_map = {
            "0° (רגיל)": 0, "90° (ימינה)": 270,
            "180° (הפוך)": 180, "270° (שמאלה)": 90
        }
        selected_angle_for_pil = angle_map[message.text]
        log_user_action(user_id, username, f"Selected rotation: {message.text} (Mapped to {selected_angle_for_pil}° for PIL)")

        processing_files[user_id]['rotation'] = selected_angle_for_pil

        bot.reply_to(
            message,
            f"קיבלתי. מתחיל עיבוד PDF עם סיבוב של {message.text}. התהליך עשוי לקחת זמן בהתאם לגודל הקובץ...",
            reply_markup=types.ReplyKeyboardRemove()
        )

        bot.send_chat_action(message.chat.id, 'upload_document')
        process_pdf_parallel(message, user_id)

    except Exception as e:
        log_user_action(user_id, username, f"Error handling rotation selection: {str(e)}\n{traceback.format_exc()}")
        bot.reply_to(message, f"אירעה שגיאה בבחירת הסיבוב: {str(e)}", reply_markup=types.ReplyKeyboardRemove())
        if user_id in processing_files:
            del processing_files[user_id]

def process_pdf_parallel(message, user_id):
    if user_id not in processing_files:
        log_user_action(user_id, message.from_user.username or "Unknown", "Error: No processing state found for parallel processing.")
        bot.send_message(message.chat.id, "שגיאה פנימית: מידע עיבוד לא נמצא. אנא נסה לשלוח את הקובץ מחדש.")
        return

    file_data = processing_files[user_id]
    pdf_path = file_data['pdf_path']
    results_dir = file_data['results_dir']
    language = file_data['language']
    rotation_angle = file_data['rotation']
    original_name = file_data['original_name']
    pdf_filename_base = os.path.splitext(file_data['pdf_filename'])[0]
    username = message.from_user.username or "Unknown"
    
    result_file_path = os.path.join(results_dir, f'{pdf_filename_base}.txt')

    log_user_action(user_id, username, f"Starting parallel processing for PDF: {original_name} (Lang: {language}, Angle: {rotation_angle}°)")
    start_time = datetime.now()

    page_results_dict = {}
    page_submission_futures = {}

    doc = None
    try:
        doc = fitz.open(pdf_path)
        num_pages = doc.page_count
        if num_pages == 0:
             log_user_action(user_id, username, f"PDF {original_name} has 0 pages. Aborting.")
             bot.send_message(message.chat.id, "הקובץ PDF ריק או פגום, לא ניתן לעבד.")
             if doc: doc.close()
             if user_id in processing_files: del processing_files[user_id]
             return

        log_user_action(user_id, username, f"PDF has {num_pages} pages. Submitting tasks to pool (Max workers: {MAX_OCR_WORKERS})...")
        
        with ProcessPoolExecutor(max_workers=MAX_OCR_WORKERS) as executor:
            for i in range(num_pages):
                page = doc.load_page(i)
                pix = page.get_pixmap(dpi=300)
                img_bytes = pix.tobytes("png")
                
                if not img_bytes:
                     log_user_action(user_id, username, f"Warning: Could not get image bytes for page {i+1}")
                     page_results_dict[i + 1] = f"--- Error getting image for page {i + 1} ---"
                     continue

                future = executor.submit(
                    process_page_ocr,
                    i,
                    img_bytes,
                    language,
                    rotation_angle,
                    EFFECTIVE_TESSERACT_CMD
                )
                page_submission_futures[future] = i + 1

            doc.close()
            doc = None
            log_user_action(user_id, username, f"Submitted {len(page_submission_futures)} pages. Waiting for completion...")

            processed_count = 0
            total_tasks = len(page_submission_futures)
            for future in as_completed(page_submission_futures):
                page_num_1_based = page_submission_futures[future]
                try:
                    _returned_page_num, text_content = future.result()
                    page_results_dict[page_num_1_based] = text_content
                except Exception as exc:
                    log_user_action(user_id, username, f'Error processing page {page_num_1_based} (future result): {exc}\n{traceback.format_exc()}')
                    page_results_dict[page_num_1_based] = f"--- Error processing page {page_num_1_based}: {exc} ---"
                
                processed_count += 1
                if processed_count % 5 == 0 or processed_count == total_tasks:
                     progress = (processed_count / total_tasks) * 100
                     log_user_action(user_id, username, f"OCR Processing progress: {processed_count}/{total_tasks} pages ({progress:.1f}%)")


        log_user_action(user_id, username, "All pages processed by pool. Assembling final text file.")
        with open(result_file_path, 'w', encoding='utf-8') as combined_file:
            for i in range(1, num_pages + 1):
                page_text = page_results_dict.get(i, f"--- Text for page {i} was not processed or result missing ---")
                combined_file.write(f"\n\n===== PAGE {i} =====\n\n")
                combined_file.write(page_text)

        log_user_action(user_id, username, f"Processing complete. Sending result file: {result_file_path}")
        with open(result_file_path, 'rb') as txt_file_to_send:
            bot.send_document(message.chat.id, txt_file_to_send, caption=f"טקסט שחולץ מתוך: {original_name}.txt")

        end_time = datetime.now()
        duration = end_time - start_time
        log_user_action(user_id, username, f"Successfully completed processing PDF '{original_name}' in {duration}.")
        bot.send_message(message.chat.id, "✅ עיבוד הקובץ הושלם!")

    except fitz.fitz.FileNotFoundError:
         log_user_action(user_id, username, f"Error: PDF file not found at path during processing: {pdf_path}")
         bot.send_message(message.chat.id, "שגיאה: קובץ ה-PDF המקורי נמחק או הועבר לפני שהעיבוד הסתיים.")
    except Exception as e:
        log_user_action(user_id, username, f"Fatal error during parallel PDF processing: {str(e)}\n{traceback.format_exc()}")
        bot.send_message(message.chat.id, f"❌ אירעה שגיאה חמורה במהלך עיבוד הקובץ: {str(e)}")
    finally:
        if doc:
            doc.close()
        if user_id in processing_files:
            del processing_files[user_id]
        log_user_action(user_id, username, "Cleaned up processing state for user.")


if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Bot starting...")
    
    while True:
        try:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Starting polling...")
            bot.polling(none_stop=True, interval=1, timeout=60) 
        except requests.exceptions.ConnectionError as ce:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Network Connection Error during polling: {ce}")
            traceback.print_exc()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Restarting polling in 30 seconds due to connection error...")
            time.sleep(30)
        except requests.exceptions.ReadTimeout as rt:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Read Timeout Error during polling: {rt}")
            traceback.print_exc()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Restarting polling in 10 seconds due to read timeout...")
            time.sleep(10)
        except Exception as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Generic Bot polling error: {e}")
            traceback.print_exc()
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Restarting polling in 15 seconds...")
            time.sleep(15)
