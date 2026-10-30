import os
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI

# Find .env in the same folder as this script
env_path = Path(__file__).resolve().parent / ".env"

# Load NVIDIA configuration
load_dotenv(dotenv_path=env_path, override=True)

print("Model loaded:", os.getenv("NVIDIA_MODEL"))

api_key = os.getenv("NVIDIA_API_KEY", "").strip()

# Check without displaying the secret key
print("Environment file found:", env_path.is_file())
print("NVIDIA API key loaded:", bool(api_key))

if not api_key:
    raise RuntimeError("NVIDIA_API_KEY is missing or empty.")

# Connect to NVIDIA API
client = OpenAI(
    base_url="https://integrate.api.nvidia.com/v1",
    api_key=api_key
)
response = client.chat.completions.create(
    model=os.getenv("NVIDIA_MODEL"),
    messages=[
        {
            "role": "system",
            "content": "You are LastBite, a helpful food rescue assistant. Give concise, practical recipes."
        },
        {
            "role": "user",
            "content": "Suggest a recipe using tomatoes, onions and potatoes."
        }
    ],
    temperature=0.3,
    max_tokens=2048
)

message = response.choices[0].message

print("\n=== NVIDIA NEMOTRON RESPONSE ===")

print("Content:", message.content)
print("Finish reason:", response.choices[0].finish_reason)

if message.content:
    print("\nSUCCESS! Nemotron generated a response.")
else:
    print("\nNo final text returned. Check finish reason and model output.")
print(response.choices[0].message.content)