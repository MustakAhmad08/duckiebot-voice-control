"""
Copy this file to config.py and fill in your local credentials.
Do not commit config.py.
"""

import os

os.environ["AZURE_SPEECH_KEY"] = "your-azure-speech-key"
os.environ["AZURE_SPEECH_REGION"] = "eastus"

os.environ["AZURE_OPENAI_KEY"] = "your-azure-openai-key"
os.environ["AZURE_OPENAI_ENDPOINT"] = "https://your-resource.openai.azure.com/"
os.environ["AZURE_OPENAI_DEPLOYMENT"] = "gpt-4o"
