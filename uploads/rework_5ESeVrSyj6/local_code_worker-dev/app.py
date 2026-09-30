import os
import zipfile
import tempfile
import requests
import subprocess
import shutil
from flask import Flask, request, render_template, jsonify, send_file
from werkzeug.utils import secure_filename

from params.settings import DEFAULT_API_URL, project_url


app = Flask(__name__)

# Configuration
UPLOAD_FOLDER = 'uploads'
ALLOWED_EXTENSIONS = {'zip'}
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure upload directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    """Check if the file has an allowed extension"""
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

def process_code_files(directory):
    """Process all Python files in directory to add docstrings"""
    try:
        # This would be implemented based on the actual AI processing logic
        # For now, we'll just return a placeholder response
        return {"status": "success", "message": "Docstrings added to Python files"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def run_ai_processing(api_url, model, prompt, code_directory):
    """Run AI processing on code files"""
    try:
        # This would make API calls to the AI service
        # For now, we'll simulate processing
        return {"status": "success", "message": f"Processed with model {model} using prompt: {prompt}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

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
        porject_url = request.form.get('porject_url', project_url) + '/archive/refs/heads/dev.zip'
        model = request.form.get('model', 'qwen3-coder-30b-a3b-instruct')
        password = request.form.get('password', '')
        prompt = request.form.get('prompt', '')
        
        # Validate required parameters
        if not all([api_url, porject_url, prompt]):
            return jsonify({"status": "error", "message": "Missing required parameters"}), 400
        
        # Download archive
        archive_filename = secure_filename(f"archive_{os.getpid()}.zip")
        archive_path = os.path.join(app.config['UPLOAD_FOLDER'], archive_filename)
        
        try:
            response = requests.get(porject_url, stream=True)
            response.raise_for_status()
            
            with open(archive_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Failed to download archive: {str(e)}"}), 500
        
        # Extract archive
        extract_dir = tempfile.mkdtemp(prefix='code_process_')
        
        if not extract_archive(archive_path, extract_dir):
            return jsonify({"status": "error", "message": "Failed to extract archive"}), 500
        
        # Process code files
        result = process_code_files(extract_dir)
        
        # Run AI processing if needed
        if api_url and model:
            ai_result = run_ai_processing(api_url, model, prompt, extract_dir)
            result.update(ai_result)
        
        # Create result archive
        result_filename = f"processed_{os.getpid()}.zip"
        result_path = os.path.join(app.config['UPLOAD_FOLDER'], result_filename)
        
        with zipfile.ZipFile(result_path, 'w') as zipf:
            for root, dirs, files in os.walk(extract_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arc_path = os.path.relpath(file_path, extract_dir)
                    zipf.write(file_path, arc_path)
        
        # Clean up temporary directories
        try:
            shutil.rmtree(extract_dir)
            os.remove(archive_path)
        except Exception:
            pass
        
        # Return download link
        return jsonify({
            "status": "success",
            "message": "Processing completed",
            "download_url": f"/download/{result_filename}"
        })
        
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