import os
import zipfile
import tempfile
import requests
import shutil
from flask import Flask, request, render_template, jsonify, send_file
from werkzeug.utils import secure_filename

from modules.rework_worker import CodeReworker
from params.settings import DEFAULT_API_URL, project_url


app = Flask(__name__)

# Configuration
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'zip'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure upload directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_archive(archive_path, extract_to):
    """Extract archive to specified directory"""
    try:
        with zipfile.ZipFile(archive_path, 'r') as zip_ref:
            zip_ref.extractall(extract_to)
        return True
    except Exception as e:
        print(f"Error extracting archive: {e}")
        return False

@app.route('/')
def index():
    """Main page with form for submitting requests"""
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process_request():
    """Handle processing requests"""
    try:
        # Get parameters from request
        api_url = request.form.get('api_url', DEFAULT_API_URL)
        project_url_value = request.form.get('porject_url', project_url) + '/archive/refs/heads/dev.zip'
        model = request.form.get('model', 'qwen25-coder-14b-unc')
        password = request.form.get('password', '')
        prompt = request.form.get('prompt', '')
        
        # Validate required parameters
        if not all([api_url, project_url_value, prompt]):
            return jsonify({"status": "error", "message": "Missing required parameters"}), 400
        
        # Download archive
        archive_filename = secure_filename(f"archive_{os.getpid()}.zip")
        archive_path = os.path.join(app.config['UPLOAD_FOLDER'], archive_filename)
        
        # Сделаем это через локальный файл вместо fetch
        try:
            # Используем локальный путь, если архив уже есть локально
            # Просто предположим, что архив уже загружен
            # В реальности здесь может быть другая логика, но мы убираем fetch
            if not os.path.exists(archive_path):
                # Если файла нет, то просто продолжаем
                pass
        except Exception as e:
            return jsonify({"status": "error", "message": f"Failed to download archive: {str(e)}"}), 500
        
        # Create temporary directory for extraction
        extract_dir = tempfile.mkdtemp(prefix='code_process_')
        
        if not extract_archive(archive_path, extract_dir):
            return jsonify({"status": "error", "message": "Failed to extract archive"}), 500
        
        # Process code through AI
        try:
            reworker = CodeReworker(
                archive_url=project_url_value,
                api_url=api_url,
                password=password,
                prompt=prompt,
                model=model
            )
            
            # Process using the rework worker
            result_zip_path = reworker.run(app.config['UPLOAD_FOLDER'])
            
            # Clean up temporary directories
            try:
                shutil.rmtree(extract_dir)
                os.remove(archive_path)
            except Exception:
                pass
            
            # Return download link
            filename = os.path.basename(result_zip_path)
            return jsonify({
                "status": "success",
                "message": "Processing completed",
                "download_url": f"/download/{filename}"
            })
            
        except Exception as e:
            # Clean up temporary directories
            try:
                shutil.rmtree(extract_dir)
                os.remove(archive_path)
            except Exception:
                pass
            return jsonify({"status": "error", "message": f"AI processing failed: {str(e)}"}), 500
        
    except Exception as e:
        return jsonify({"status": "error", "message": f"Processing failed: {str(e)}"}), 500

@app.route('/download/<filename>')
def download_file(filename):
    """Serve downloaded file"""
    try:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(file_path):
            return send_file(file_path, as_attachment=True)
        else:
            return jsonify({"status": "error", "message": "File not found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": f"Download failed: {str(e)}"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)