import telebot
from pdf2image import convert_from_path, pdfinfo_from_path
import pytesseract
from PIL import Image
import os
from datetime import datetime
import json
from telebot import types
from concurrent.futures import ProcessPoolExecutor
import numpy as np
import cv2

with open('config.json') as config_file:
   config = json.load(config_file)
   
bot = telebot.TeleBot(config['token'])
BASE_DIR = "bot_storage"
USERS_DIR = os.path.join(BASE_DIR, "users")
SUPPORTED_LANGUAGES = {'עברית': 'heb', 'אנגלית': 'eng', 'רוסית': 'rus'}
processing_files = {}

if not os.path.exists(BASE_DIR):
   os.makedirs(BASE_DIR)
if not os.path.exists(USERS_DIR):
   os.makedirs(USERS_DIR)

def get_user_directories(user_id):
   user_base_dir = os.path.join(USERS_DIR, str(user_id))
   user_pdfs_dir = os.path.join(user_base_dir, "pdfs")
   user_output_dir = os.path.join(user_base_dir, "output_images")
   user_results_dir = os.path.join(user_base_dir, "results")
   for directory in [user_base_dir, user_pdfs_dir, user_output_dir, user_results_dir]:
       if not os.path.exists(directory):
           os.makedirs(directory)
   return user_pdfs_dir, user_output_dir, user_results_dir

def correct_page_orientation(image, language):
    opencv_img = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    angles = [0, 90, 180, 270]
    best_score = -1
    best_img = image
    
    for angle in angles:
        if angle == 0:
            rotated = opencv_img
        else:
            (h, w) = opencv_img.shape[:2]
            center = (w // 2, h // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)
            rotated = cv2.warpAffine(opencv_img, M, (w, h),
                                   flags=cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_REPLICATE)
        
        pil_img = Image.fromarray(cv2.cvtColor(rotated, cv2.COLOR_BGR2RGB))
        
        try:
            try:
                osd = pytesseract.image_to_osd(pil_img, output_type=pytesseract.Output.DICT)
                orientation_conf = float(osd['orientation_conf'])
            except:
                orientation_conf = 0
                
            text = pytesseract.image_to_string(pil_img, lang=language)
            
            words = [w for w in text.split() if any(c.isalnum() for c in w)]
            readable_score = len(words)
            special_chars = sum(1 for c in text if not c.isalnum() and not c.isspace())
            
            total_score = readable_score * 10 - special_chars + orientation_conf
            
            if total_score > best_score:
                best_score = total_score
                best_img = pil_img
                
        except Exception as e:
            continue
    
    return best_img

@bot.message_handler(commands=['start'])
def send_welcome(message):
   user_id = message.from_user.id
   get_user_directories(user_id)
   bot.reply_to(message, "שלום! שלח/י לי קובץ PDF ואחלץ ממנו את הטקסט.")

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

def ask_orientation_correction(message, selected_language):
   markup = types.ReplyKeyboardMarkup(row_width=2, one_time_keyboard=True)
   markup.add(
       types.KeyboardButton("המשך רגיל"),
       types.KeyboardButton("תקן כיוון דף")
   )
   bot.reply_to(message, "האם לתקן את כיוון הדפים?", reply_markup=markup)

@bot.message_handler(func=lambda message: message.text in SUPPORTED_LANGUAGES.keys())
def handle_language_selection(message):
   try:
       user_id = message.from_user.id
       if user_id not in processing_files:
           bot.reply_to(message, "אנא שלח/י קודם קובץ PDF.")
           return
       
       selected_language = SUPPORTED_LANGUAGES[message.text]
       processing_files[user_id]['language'] = selected_language
       ask_orientation_correction(message, selected_language)
       
   except Exception as e:
       bot.reply_to(message, f"אירעה שגיאה: {str(e)}")

@bot.message_handler(func=lambda message: message.text in ["המשך רגיל", "תקן כיוון דף"])
def handle_orientation_choice(message):
   try:
       user_id = message.from_user.id
       if user_id not in processing_files:
           bot.reply_to(message, "אנא שלח/י קודם קובץ PDF.")
           return
           
       file_info = processing_files[user_id]
       fix_orientation = message.text == "תקן כיוון דף"
       
       bot.reply_to(message, "מעבד את הקובץ... אנא המתן/י.", reply_markup=types.ReplyKeyboardRemove())
       
       pdf_name = os.path.splitext(file_info['pdf_filename'])[0]
       process_single_pdf(
           pdf_path=file_info['pdf_path'],
           output_dir=file_info['output_dir'],
           results_dir=file_info['results_dir'],
           pdf_name=pdf_name,
           language=file_info['language'],
           fix_orientation=fix_orientation
       )
       
       result_file_path = os.path.join(file_info['results_dir'], pdf_name, f'{pdf_name}.txt')
       with open(result_file_path, 'rb') as txt_file:
           bot.send_document(message.chat.id, txt_file, caption=file_info['original_name'])
       del processing_files[user_id]
       
   except Exception as e:
       bot.reply_to(message, f"אירעה שגיאה: {str(e)}")

def process_single_pdf(pdf_path, output_dir, results_dir, pdf_name, language='heb', fix_orientation=False):
   pdf_output_dir = os.path.join(output_dir, pdf_name)
   pdf_results_dir = os.path.join(results_dir, pdf_name)
   if not os.path.exists(pdf_output_dir):
       os.makedirs(pdf_output_dir)
   if not os.path.exists(pdf_results_dir):
       os.makedirs(pdf_results_dir)
   image_paths = pdf_to_images(pdf_path, pdf_output_dir)
   if image_paths:
       extract_hebrew_text(image_paths, pdf_results_dir, pdf_name, language, fix_orientation)

def convert_page_range(pdf_path, output_dir, start, end):
   images = convert_from_path(
       pdf_path,
       poppler_path=config['poppler_path'],
       dpi=150,
       first_page=start,
       last_page=end,
       fmt='png',
       thread_count=1,
       grayscale=True,
       size=(1700, None)
   )
   image_paths = []
   for j, image in enumerate(images, start):
       path = os.path.join(output_dir, f'page_{j}.png')
       image.save(path)
       image_paths.append(path)
   return image_paths

def pdf_to_images(pdf_path, output_dir):
   info = pdfinfo_from_path(pdf_path, poppler_path=config['poppler_path'])
   total_pages = info["Pages"]
   pages_per_worker = 10
   ranges = [(i, min(i + pages_per_worker - 1, total_pages)) for i in range(1, total_pages + 1, pages_per_worker)]
   image_paths = []
   with ProcessPoolExecutor() as executor:
       futures = [executor.submit(convert_page_range, pdf_path, output_dir, start, end) for start, end in ranges]
       for future in futures:
           image_paths.extend(future.result())
   return image_paths

def extract_hebrew_text(image_paths, results_dir, pdf_name, language='heb', fix_orientation=False):
    pytesseract.pytesseract.tesseract_cmd = config['tesseract_path']
    
    combined_output_path = os.path.join(results_dir, f'{pdf_name}.txt')
    with open(combined_output_path, 'w', encoding='utf-8') as combined_file:
        for i, image_path in enumerate(image_paths, 1):
            img = Image.open(image_path)
            
            if fix_orientation:
                try:
                    img = correct_page_orientation(img, language)
                except Exception as e:
                    print(f"Warning: Could not auto-rotate page {i}: {str(e)}")
            
            text = pytesseract.image_to_string(img, lang=language)
            page_output_path = os.path.join(results_dir, f'page_{i}.txt')
            with open(page_output_path, 'w', encoding='utf-8') as f:
                f.write(text)
            combined_file.write(text)

if __name__ == "__main__":
   bot.polling(none_stop=True)