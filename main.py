import telebot
from pdf2image import convert_from_path
import pytesseract
from PIL import Image
import os
from datetime import datetime
import json
from telebot import types
from concurrent.futures import ProcessPoolExecutor

with open('config.json') as config_file:
    config = json.load(config_file)
   
bot = telebot.TeleBot(config['token'])
BASE_DIR = "bot_storage"
USERS_DIR = os.path.join(BASE_DIR, "users")
SUPPORTED_LANGUAGES = {'עברית': 'heb', 'אנגלית': 'eng', 'רוסית': 'rus'}
processing_files = {}

for directory in [BASE_DIR, USERS_DIR]:
    if not os.path.exists(directory):
        os.makedirs(directory)

def get_user_directories(user_id):
    user_base_dir = os.path.join(USERS_DIR, str(user_id))
    user_pdfs_dir = os.path.join(user_base_dir, "pdfs")
    user_output_dir = os.path.join(user_base_dir, "output_images")
    user_results_dir = os.path.join(user_base_dir, "results")
    for directory in [user_base_dir, user_pdfs_dir, user_output_dir, user_results_dir]:
        if not os.path.exists(directory):
            os.makedirs(directory)
    return user_pdfs_dir, user_output_dir, user_results_dir

def rotate_image(img, angle):
    if angle == 0:
        return img
    return img.rotate(angle, expand=True)

def enhance_image_for_ocr(image):
    if image.mode != 'L':
        image = image.convert('L')
    
    image = image.point(lambda x: 0 if x < 128 else 255, '1')
    
    return image

@bot.message_handler(commands=['start'])
def send_welcome(message):
    user_id = message.from_user.id
    get_user_directories(user_id)
    bot.reply_to(message, "שלום! שלח/י לי קובץ PDF ואחלץ ממנו את הטקסט.")

@bot.message_handler(commands=['myid'])
def get_user_id(message):
    user_id = message.from_user.id
    bot.reply_to(message, f"Your Telegram ID is: {user_id}")

@bot.message_handler(content_types=['document'])
def handle_pdf(message):
    try:
        if message.document.mime_type != 'application/pdf':
            bot.reply_to(message, "אנא שלח/י קובץ PDF.")
            return
        
        user_id = message.from_user.id
        user_pdfs_dir, user_output_dir, user_results_dir = get_user_directories(user_id)
        
        file_info = bot.get_file(message.document.file_id)
        original_filename = message.document.file_name
        original_name = os.path.splitext(original_filename)[0]
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_filename = f"{timestamp}_{original_filename}"
        pdf_path = os.path.join(user_pdfs_dir, pdf_filename)
        
        downloaded_file = bot.download_file(file_info.file_path)
        with open(pdf_path, 'wb') as new_file:
            new_file.write(downloaded_file)
        
        processing_files[user_id] = {
            'pdf_path': pdf_path,
            'original_name': original_name,
            'pdf_filename': pdf_filename,
            'output_dir': user_output_dir,
            'results_dir': user_results_dir
        }
        
        ask_language(message)
        
    except Exception as e:
        bot.reply_to(message, f"אירעה שגיאה: {str(e)}")

def ask_language(message):
    markup = types.ReplyKeyboardMarkup(row_width=1, one_time_keyboard=True)
    language_buttons = [types.KeyboardButton(lang) for lang in SUPPORTED_LANGUAGES.keys()]
    markup.add(*language_buttons)
    bot.reply_to(message, "באיזו שפה כתוב המסמך?", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text in SUPPORTED_LANGUAGES.keys())
def handle_language_selection(message):
    try:
        user_id = message.from_user.id
        if user_id not in processing_files:
            bot.reply_to(message, "אנא שלח/י קודם קובץ PDF.")
            return
        
        selected_language = SUPPORTED_LANGUAGES[message.text]
        processing_files[user_id]['language'] = selected_language
        
        send_first_page_preview(message, user_id)
        
    except Exception as e:
        bot.reply_to(message, f"אירעה שגיאה: {str(e)}")

def send_first_page_preview(message, user_id):
    file_info = processing_files[user_id]
    pdf_path = file_info['pdf_path']
    
    try:
        images = convert_from_path(
            pdf_path,
            poppler_path=config.get('poppler_path'),
            dpi=150,
            first_page=1,
            last_page=1
        )
        
        if not images:
            bot.reply_to(message, "לא ניתן לחלץ עמודים מה-PDF.")
            return
        
        first_page = images[0]
        preview_path = os.path.join(file_info['output_dir'], "preview.jpg")
        first_page.save(preview_path, "JPEG")
        
        with open(preview_path, 'rb') as preview_file:
            bot.send_photo(
                message.chat.id, 
                preview_file, 
                caption="הנה תצוגה מקדימה של העמוד הראשון. אנא בחר/י את כיוון הדף הנכון:"
            )
        
        ask_rotation(message)
        
    except Exception as e:
        bot.reply_to(message, f"אירעה שגיאה בהכנת תצוגה מקדימה: {str(e)}")

def ask_rotation(message):
    markup = types.ReplyKeyboardMarkup(row_width=2, one_time_keyboard=True)
    markup.add(
        types.KeyboardButton("0° (רגיל)"),
        types.KeyboardButton("90° (ימינה)"),
        types.KeyboardButton("180° (הפוך)"),
        types.KeyboardButton("270° (שמאלה)")
    )
    bot.reply_to(message, "באיזו זווית יש לסובב את כל העמודים?", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text in ["0° (רגיל)", "90° (ימינה)", "180° (הפוך)", "270° (שמאלה)"])
def handle_rotation_selection(message):
    try:
        user_id = message.from_user.id
        if user_id not in processing_files:
            bot.reply_to(message, "אנא שלח/י קודם קובץ PDF.")
            return
        
        angle_map = {
            "0° (רגיל)": 0,
            "90° (ימינה)": 90,
            "180° (הפוך)": 180,
            "270° (שמאלה)": 270
        }
        
        selected_angle = angle_map[message.text]
        processing_files[user_id]['rotation'] = selected_angle
        
        status_message = bot.reply_to(
            message, 
            f"מעבד את הקובץ עם סיבוב של {selected_angle}°... אנא המתן/י.", 
            reply_markup=types.ReplyKeyboardRemove()
        )
        
        process_pdf_with_rotation(message, user_id, selected_angle)
        
    except Exception as e:
        bot.reply_to(message, f"אירעה שגיאה: {str(e)}")

def process_pdf_with_rotation(message, user_id, angle):
    try:
        file_info = processing_files[user_id]
        pdf_path = file_info['pdf_path']
        output_dir = file_info['output_dir']
        results_dir = file_info['results_dir']
        language = file_info['language']
        
        pdf_name = os.path.splitext(file_info['pdf_filename'])[0]
        pdf_output_dir = os.path.join(output_dir, pdf_name)
        pdf_results_dir = os.path.join(results_dir, pdf_name)
        
        if not os.path.exists(pdf_output_dir):
            os.makedirs(pdf_output_dir)
        if not os.path.exists(pdf_results_dir):
            os.makedirs(pdf_results_dir)
        
        image_paths = pdf_to_images(pdf_path, pdf_output_dir)
        
        if image_paths:
            extract_text_with_rotation(image_paths, pdf_results_dir, pdf_name, language, angle)
            
            result_file_path = os.path.join(pdf_results_dir, f'{pdf_name}.txt')
            with open(result_file_path, 'rb') as txt_file:
                bot.send_document(message.chat.id, txt_file, caption=file_info['original_name'])
            
            bot.send_message(message.chat.id, "עיבוד הקובץ הושלם!")
        else:
            bot.reply_to(message, "לא הצלחתי לחלץ עמודים מהקובץ.")
        
    except Exception as e:
        bot.reply_to(message, f"אירעה שגיאה בעיבוד הקובץ: {str(e)}")

def pdf_to_images(pdf_path, output_dir):
    try:
        info = convert_from_path(
            pdf_path, 
            poppler_path=config.get('poppler_path'),
            first_page=1,
            last_page=1
        )
        
        images = convert_from_path(
            pdf_path,
            poppler_path=config.get('poppler_path'),
            dpi=300,
            fmt='png'
        )
        
        image_paths = []
        for i, image in enumerate(images, 1):
            path = os.path.join(output_dir, f'page_{i}.png')
            image.save(path)
            image_paths.append(path)
        
        return image_paths
    
    except Exception as e:
        print(f"Error extracting images from PDF: {str(e)}")
        return []

def extract_text_with_rotation(image_paths, results_dir, pdf_name, language='heb', rotation_angle=0):
    if 'tesseract_path' in config:
        pytesseract.pytesseract.tesseract_cmd = config['tesseract_path']
    
    combined_output_path = os.path.join(results_dir, f'{pdf_name}.txt')
    
    with open(combined_output_path, 'w', encoding='utf-8') as combined_file:
        for i, image_path in enumerate(image_paths, 1):
            img = Image.open(image_path)
            
            original_path = os.path.join(results_dir, f'original_page_{i}.png')
            img.save(original_path)
            
            rotated_img = rotate_image(img, rotation_angle)
            rotated_path = os.path.join(results_dir, f'rotated_page_{i}.png')
            rotated_img.save(rotated_path)
            
            enhanced_img = enhance_image_for_ocr(rotated_img)
            enhanced_path = os.path.join(results_dir, f'enhanced_page_{i}.png')
            enhanced_img.save(enhanced_path)
            
            custom_config = f'--oem 3 --psm 6 -l {language}'
            text = pytesseract.image_to_string(enhanced_img, lang=language, config=custom_config)
            
            page_output_path = os.path.join(results_dir, f'page_{i}.txt')
            with open(page_output_path, 'w', encoding='utf-8') as f:
                f.write(text)
            
            page_text = f"\n\n===== PAGE {i} =====\n\n{text}"
            combined_file.write(page_text)

if __name__ == "__main__":
    bot.polling(none_stop=True)