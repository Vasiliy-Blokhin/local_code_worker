// static/app.js - Updated to show logs and handle form properly
document.getElementById('processForm').addEventListener('submit', async function(e) {
    e.preventDefault();
    
    const form = e.target;
    const formData = new FormData(form);
    
    const resultDiv = document.getElementById('result');
    resultDiv.innerHTML = '<p>Processing...</p>';
    
    // Clear previous logs
    const logsDiv = document.getElementById('logs');
    logsDiv.innerHTML = '<p>Processing logs:</p>';
    
    try {
        const response = await fetch('/process', {
            method: 'POST',
            body: formData
        });
        
        const result = await response.json();
        
        if (result.status === 'success') {
            resultDiv.innerHTML = `
                <div class="success">
                    <p>${result.message}</p>
                    <a href="${result.download_url}">Download Processed Archive</a>
                </div>
            `;
        } else {
            resultDiv.innerHTML = `
                <div class="error">
                    <p>Error: ${result.message}</p>
                </div>
            `;
        }
    } catch (error) {
        resultDiv.innerHTML = `
            <div class="error">
                <p>Error: ${error.message}</p>
            </div>
        `;
    }
});