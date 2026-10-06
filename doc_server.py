# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import base64
from io import BytesIO
import threading
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from openai import OpenAI
from pypdf import PdfReader
from pdf2image import convert_from_path
from PIL import Image

# --- НАСТРОЙКИ ---
PORT = 8094
LOG_FILE = "/opt/doc_service/ai_service.log"
NETWORK_SHARE = "/opt/doc_service/network_share"
VSE_GPT_API_KEY = "sk-or-vis-b75a42-cc3d5cf9d17a69b2f71d64f2f8b5a2b329875162a3126176ff67fd94b120738c"  
BASE_URL = "https://api.vsegpt.ru/v1"
MODEL_NAME = "anthropic/claude-sonnet-5" #"openai/gpt-4o"  # Тяжелая модель с лимитом 16k токенов на вывод

app = FastAPI()

def log_message(text: str):
    """Synchronous secure logging function"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    clean_text = str(text).encode('utf-8', errors='replace').decode('utf-8')
    log_line = f"[{timestamp}] {clean_text}\n"
    print(log_line.strip())
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception:
        pass

def repair_mojibake(text: str) -> str:
    """Fixes the weird characters and Unicode escaping issues from FiveWin"""
    if not text:
        return ""
    # Если строка пришла в виде \u04xx (юникод-экранирование), декодируем её
    if "\\u" in text or u"\u0400" in text:
        try:
            return text.encode('utf-8').decode('unicode_escape')
        except Exception:
            try:
                return json.loads(f'"{text}"')
            except Exception:
                pass
    try:
        bytes_str = text.encode('raw_unicode_escape')
        return bytes_str.decode('utf-8')
    except Exception:
        try:
            return text.encode('cp1251').decode('utf-8')
        except Exception:
            return text

def encode_image_to_base64(pil_img: Image.Image) -> str:
    """Converts PIL Image to a Base64 JPEG string for transmission Vision API"""
    buffered = BytesIO()
    pil_img.thumbnail((1600, 1600))
    pil_img.save(buffered, format="JPEG", quality=80)
    return base64.b64encode(buffered.getvalue()).decode('utf-8')

def extract_content_from_file(file_path: str):
    """Analyzes the file. Highlights text or renders the PDF into images if it a scan."""
    ext = os.path.splitext(file_path)[1].lower()
    
    if ext in ['.png', '.jpg', '.jpeg', '.tiff', '.bmp']:
        try:
            with Image.open(file_path) as img:
                b64 = encode_image_to_base64(img)
                return {'type': 'images', 'data': [b64]}
        except Exception as e:
            log_message(f"Error processing image {file_path}: {e}")
            return {'type': 'text', 'data': f"[Error loading image: {file_path}]"}

    elif ext == '.pdf':
        try:
            reader = PdfReader(file_path)
            full_text = ""
            for page in reader.pages:
                t = page.extract_text()
                if t:
                    full_text += t + "\n"
            
            if len(full_text.strip()) > 150:
                log_message(f"PDF {os.path.basename(file_path)} parsed as TEXT layer.")
                return {'type': 'text', 'data': full_text}
            
            log_message(f"PDF {os.path.basename(file_path)} has no text layer. OCR Mode...")
            images = convert_from_path(file_path, dpi=130)
            b64_list = []
            for img in images:
                b64_list.append(encode_image_to_base64(img))
            return {'type': 'images', 'data': b64_list}
            
        except Exception as e:
            log_message(f"Error parsing PDF {file_path}: {e}")
            return {'type': 'text', 'data': f"[Error loading PDF: {file_path}]"}
    else:
        return {'type': 'text', 'data': f"[Unsupported file type: {ext}]"}

#ЧАСТЬ 2 (Скопируйте и вставьте сразу после первой части, без пустых строк):
def async_ai_processing(task_id: str, instruction: str):
    """The background, heavy task of processing documents and requests VseGPT"""
    log_message(f"--- START BACKGROUND TASK FOR USER: {task_id} ---")
    user_folder = os.path.join(NETWORK_SHARE, task_id)
    log_message(f"Checking path: {user_folder}")
    
    if not os.path.exists(user_folder):
        log_message(f"ERROR: Directory {user_folder} does not exist.")
        log_message(f"--- END BACKGROUND TASK FOR USER: {task_id} ---")
        return

    raw_files = os.listdir(user_folder)
    # Оставляем имена файлов строго в том сыром байтовом виде, в каком их вернула ОС Linux
    files_in_folder = [f for f in raw_files if not f.startswith("result.")]
    log_message(f"[TRACE] Raw files found on disk: {files_in_folder}")
    
    text_contents = []
    image_b64_contents = []
    
    for file_name in files_in_folder:
        full_path = os.path.join(user_folder, file_name)

        wait_timeout = 30
        start_wait = time.time()
        file_ready = False
        while time.time() - start_wait < wait_timeout:
            try:
                with open(full_path, 'rb+') as f:
                    file_ready = True
                break
            except (PermissionError, IOError):
                time.sleep(0.5)
        if not file_ready:
            log_message(f"[ERROR] The {file_name} file is occupied by the Windows client. Skip it.")
            continue
        log_message(f"Processing file: {file_name}")
        
        # Оборачиваем вызов парсера в перехватчик ошибок, чтобы защитить поток от бесшумного падения
        try:
            result = extract_content_from_file(full_path)
            if result['type'] == 'text':
                text_contents.append(f"--- File: {file_name} ---\n{result['data']}\n")
            elif result['type'] == 'images':
                image_b64_contents.extend(result['data'])
        except Exception as file_err:
            log_message(f"[CRITICAL ERROR] Failed to process file {file_name}: {file_err}")
    api_content = []
    full_prompt = f"User Instruction: {instruction}\n\n"
    if text_contents:
        full_prompt += "Extracted text documents content:\n" + "\n".join(text_contents)

    system_instruction = (
       "You are a professional document digitizer. Your primary task is to convert all data and tables "
        "from the provided images into text format. Please write the entire final output in Russian.\n\n"
        "Guidelines for extraction:\n"
        "1. Please transfer every single row and cell carefully. Do not use summary rows or ellipses.\n"
        "2. Keep the original table layouts, column meanings, and alignment without merging or skipping information.\n"
        "3. Format the result as a standard clean HTML table using <table> tags. Do not use Markdown characters or code blocks.\n"
        "4. If the source layout is complex, use standard HTML colspan and rowspan to preserve the exact original structure."
    )

    api_content = []
    # Объединяем англоязычную системную инструкцию и текст от пользователя
    full_prompt = f"System Instruction: {system_instruction}\n\nUser Request: {instruction}\n\n"
    if text_contents:
        full_prompt += "Extracted text documents content:\n" + "\n".join(text_contents)
    

    api_content.append({"type": "text", "text": full_prompt})
    
    for b64 in image_b64_contents:
        api_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
        })

    import requests
    import urllib3

    # Отключаем предупреждения об отсутствии SSL-проверки в логах CentOS
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    log_message(f"[TRACE] Request payload ready. Images to send: {len(image_b64_contents)}")

    log_message(f"[{task_id}] Classifying user intent and target format...")
    
    classification_prompt = (
        f"Analyze the user instruction: '{instruction}'.\n"
        "Determine the appropriate output format based on the request:\n"
        "Choose 'E' if the user wants a table, Excel spreadsheet, registry, data export, "
        "or if the context of the document strongly implies a structured tabular layout.\n"
        "Choose 'H' if the user wants text, a text report, summary, analysis, translation, or answers to questions.\n"
        "Output EXACTLY one uppercase letter: E or H. Do not include any other characters, dots, or explanations."
    )
    
    target_format = "E"
    try:
        class_res = requests.post(
            "https://vsegpt.ru",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": classification_prompt}],
                "temperature": 0.0
            },
            timeout=10
        )
        if class_res.status_code == 200:
            raw_decision = class_res.json()["choices"]["message"]["content"].strip().upper()
            if "E" in raw_decision:
                target_format = "E"
            elif "H" in raw_decision:
                target_format = "H"
            log_message(f"[{task_id}] AI classified format as: {target_format} (E = Excel, H = Text/HTML)")
        else:
            log_message(f"[{task_id}] Classification error (Status {class_res.status_code}), falling back to E")
    except Exception as ex:
        log_message(f"[{task_id}] Exception during classification: {ex}, falling back to E")
        
    final_filename = "result_E.html" if target_format == "E" else "result_H.html"


    log_message("Starting MapReduce loop for VseGPT API...")
    table_rows_accumulator = []
    
    headers = {
        "Authorization": f"Bearer {VSE_GPT_API_KEY}",
        "Content-Type": "application/json"
    }

    # Постраничный цикл обработки сканов (полный код реализации доступен по ссылке на Gist)
    for idx, b64_img in enumerate(image_b64_contents):
        page_num = idx + 1
        log_message(f"Processing page {page_num}/{len(image_b64_contents)} for task {task_id}")
        
        if target_format == "E":
            page_prompt = (
                f"System Instruction: You are a professional document digitizer. Analyze this image (page {page_num}). "
                f"Extract all tables and ALL text rows above, between, or below the tables according to this instruction: '{instruction}'. "
                "For any non-table text lines or headers, convert them into a table row spanning all columns, like: <tr><td colspan='15'><b>TEXT LINE HERE</b></td></tr>. "
                "\n\nCRITICAL VISUAL REQUIREMENT FOR ALIGNMENT:\n"
                "Analyze the horizontal position of data inside every single cell and apply direct inline CSS styles:\n"
                "1. If a cell contains numbers, amounts, dates, or prices that are right-aligned or centered in the image, add style='text-align: right;' or style='text-align: center;' to the <td> tag.\n"
                "2. If a header is centered, use <td style='text-align: center;'><b>Header</b></td>.\n"
                "3. Do not assume left alignment for everything. Replicate the visual layout strictly.\n\n"
                "Return ONLY the raw HTML table rows enclosed in <tr>...</tr> tags. Do not include <table>, <body>, or markdown blocks."
            )
        else:
            page_prompt = (
                f"System Instruction: You are an expert document analyst. Analyze this image (page {page_num}). "
                f"Fulfill the user's request: '{instruction}'.\n"
                "Generate a clean, professional textual report for this page using standard HTML tags: "
                "use <h2> or <h3> for headers, <p> for paragraphs, <ul> and <li> for bullet lists, and <b> for bold text. "
                "Do NOT use markdown symbols like asterisks (**) or hashes (#). Do NOT use <table> tags. "
                "Return ONLY the raw HTML body content for this page. Do not include <html>, <head> or <body> tags."
            )
        payload = {
            "model": MODEL_NAME,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": page_prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_img}"
                            }
                        }
                    ]                }
            ],
            "temperature": 0.2
        }

        try:
            res = requests.post(f"{BASE_URL}/chat/completions", json=payload, headers=headers, timeout=120, verify=False)
            if res.status_code == 200:
                raw_json = res.json()
                if "choices" in raw_json and len(raw_json["choices"]) > 0:
                    raw_html_chunk = raw_json["choices"][0]["message"]["content"].strip()
                    if raw_html_chunk.startswith("```"):
                        raw_html_chunk = raw_html_chunk.replace("```html", "").replace("```", "").strip()
                    table_rows_accumulator.append(raw_html_chunk)
                    log_message(f"[API OK] Page {page_num} processed successfully.")
                else:
                    err_text = f"Unexpected JSON structure from VseGPT: {raw_json}"
                    log_message(f"[API ERROR] Page {page_num} failed. Details: {err_text}")
                    table_rows_accumulator.append(f"<tr><td colspan='10'>Error: Invalid JSON response on page {page_num}</td></tr>")
            else:
                log_message(f"[API ERROR] Page {page_num} returned HTTP {res.status_code}. Response body: {res.text}")
                table_rows_accumulator.append(f"<tr><td colspan='10'>Error processing page {page_num} (HTTP {res.status_code})</td></tr>")
        except Exception as page_err:
            log_message(f"[PAGE ERROR] Page {page_num} failed with: {str(page_err)}")
            table_rows_accumulator.append(f"<tr><td colspan='10'>Exception on page {page_num}</td></tr>")

    combined_rows = "\n".join(table_rows_accumulator)
    answer_text = f"<table>\n<tbody>\n{combined_rows}\n</tbody>\n</table>"

    # Собираем дату без системного time.strftime, строго математикой Питона.
    # Это окончательно защищает конвейер от пажения utf-8 кодека.
    from datetime import datetime
    dt = datetime.now()
    current_date = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"

    template_path = "/opt/doc_service/template.html"
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as tf:
            html_template = tf.read()
    else:
        # Резервный ультра-простой вариант, если файла нет
        html_template = "<html><body><h1>Result</h1><div>{{ANSWER}}</div></body></html>"

    # Подставляем переменные безопасным строковым методом
    final_html = html_template.replace("{{TASK_ID}}", task_id)
    final_html = final_html.replace("{{INSTRUCTION}}", instruction)
    final_html = final_html.replace("{{DATE}}", current_date)
    final_html = final_html.replace("{{ANSWER}}", answer_text)

    result_path = os.path.join(user_folder, final_filename)

    # Перед записью принудительно переводим строку в чистый UTF-8 байт-массив,
    # полностью игнорируя любые системные настройки локали CentOS и Samba
    try:
        binary_html = final_html.encode('utf-8', errors='ignore')
        with open(result_path, "wb") as rf:
            rf.write(binary_html)
        log_message("SUCCESS: HTML report saved safely via binary stream.")
        import gc
        gc.collect()
        log_message("Samba handles successfully closed.")
    except Exception as write_err:
        log_message(f"CRITICAL: Binary write failed: {write_err}")
        # Резервный ультра-безопасный способ записи (ASCII-only)
        with open(result_path, "w", encoding="ascii", errors="xmlcharrefreplace") as rf:
            rf.write(final_html)
    except Exception as e:
        log_message(f"ERROR IN BACKGROUND TASK: {str(e)}")
        log_message(f"--- END BACKGROUND TASK FOR USER: {task_id} ---\n")

@app.post("/process_documents")
async def process_documents(request: Request, background_tasks: BackgroundTasks):
    try:
        body_bytes = await request.body()
        body_str = body_bytes.decode('utf-8', errors='replace')
        data = json.loads(body_str)
    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": f"Invalid JSON body: {e}"})

    # УМНОЕ ИСПРАВЛЕНО: Защита от обоих видов кодировки FiveWin (и \u04xx, и сырого CP1251)
    task_id = str(data.get("task_id", "unknown")).strip()
    instruction = str(data.get("instruction", ""))
    
    # Сценарий А: Если прилетел экранированный текст вида \u0421... или \\u0421...
    if "\\u" in instruction or u"\u0400" in instruction:
        try:
            instruction = instruction.encode('utf-8').decode('unicode_escape')
            if "\\u" in instruction:
                instruction = instruction.encode('utf-8').decode('unicode_escape')
        except Exception:
            pass
            
    if "\\u" in task_id or u"\u0400" in task_id:
        try:
            task_id = task_id.encode('utf-8').decode('unicode_escape')
            if "\\u" in task_id:
                task_id = task_id.encode('utf-8').decode('unicode_escape')
        except Exception:
            pass

    # Сценарий Б: Если прилетел битый ковер из русских букв (как в последнем тесте),
    # мы прогоняем его через наш проверенный медицинский фильтр repair_mojibake
    task_id = repair_mojibake(task_id)
    instruction = repair_mojibake(instruction)

    log_message(f"HTTP POST received. task_id: {task_id}, instruction: {instruction}")
    background_tasks.add_task(async_ai_processing, task_id, instruction)
    return {"status": "accepted", "task_id": task_id}

if __name__ == "__main__":
    import uvicorn
    log_message(f"Starting server on port {PORT}...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
