# 📄 PDFTextifyBot

A Telegram bot that extracts text from PDF files using OCR. The bot supports Hebrew, English, and Russian languages and includes optional automatic page orientation correction.

## 🚀 Features

- 📂 Upload a **PDF**, and the bot extracts text using **OCR**.
- 🌍 Supports **Hebrew**, **English**, and **Russian** via `pytesseract`.
- 🔄 **Automatic page orientation correction** using OpenCV (optional).
- 📑 **Multi-page PDF support** with parallel processing.
- 🖼️ Converts PDFs to images using `pdf2image` (Poppler required).
- 💾 Saves extracted text as a `.txt` file and sends it back.
