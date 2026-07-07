# Jarvis
Jarvis is an agentic framework built for local AI desktop assistance. It can help you with code, UIA, repetitive tasks, browsing etc. 

<img width="1280" height="800" alt="image" src="https://github.com/user-attachments/assets/b4acdd7b-5030-498c-8214-82e2b4844ec9" />

## Setup Instruction
### Step 1:
Download the ZIP file from the releases and extract it into an empty folder or clone the repo inside an empty folder
### Step 2:
Download the python libraries required for the scripts to run using the following command.
```Poweshell
pip install ollama pillow ddgs keyboard pymupdf mammoth python-docx rich pytesseract pywin32 uiautomation customtkinter google-genai requests
pip install -U gemini_webapi
pip install -U browser-cookie3   # optional but recommended
```
### Step 3:
Download Ollama and download a tooling capable model of your choice (Older models like qwen2.5-coder are also supported)
For example:
```Poweshell
ollama pull qwen2.5-coder:7b
```
### Step 4:
Change the first line of the Modelfile to configure it for the model of your choice. Currently, it is set to **qwen2.5-coder:7b**
### Step 5:
Apply the provided Modelfile to your model by running the following command **in the folder the Modelfile is located**.
```Poweshell
ollama create jarvis -f ./Modelfile
```
### Step 6:
Run main.py once. This creates all the necessary files 
### Optional Step
Download and install Tesseract as it allows for OCR to work. Jarvis is completely functional without OCR.
### Step 7:
You can now launch Jarvis. 
Run the **gui.py** script if you want to run the Jarvis Control Centre which allows you the full functionality of Jarvis while also allowing you to modify any file (Like skill files, knowledge bases, memory files etc). This is the recommended approach to use Jarvis.

Run the **main.py** script if you prefer the CLI tool instead. This requires an IDE if you want to modify any files (Or you can tell Jarvis to do it.)

## Browser Support

For reliable browser page navigation and interaction, launch your browser with **Remote Debugging** enabled.

Without Remote Debugging, Jarvis falls back to UI Automation (UIA), which works for many tasks but may be less reliable on complex web pages.

### Example (Chrome/Brave/Edge)

For Windows, use this command (Replace the path with your actual browser executable path):
```PowerShell
& "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe" --remote-debugging-port=9222
```

Jarvis will automatically use the debugging interface when available and fall back to UIA otherwise.


## Models I have tested and am happy with:
1. qwen2.5-coder:7b
2. qwen3.5:4b
## Jarvis' Capabilities
Jarvis can do the following things:
- Read/write files
- Control your desktop using UIA
- Use and create skills
- Use and create knowledge bases
- More things that I am too tired to list
