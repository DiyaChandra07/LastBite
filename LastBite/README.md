# LastBite — food-waste prevention prototype

LastBite is a responsive web app that can be installed on Android from a compatible browser as a Progressive Web App (PWA). This hackathon prototype focuses only on:

1. **SmartScan** — upload a photo of a food package, optionally read printed text with OCR, and review details before saving.
2. **My Fridge** — store, search, filter, edit and remove groceries in a local SQLite database.
3. **Smart Reminders** — show in-app reminders based on dates the user has confirmed and the reminder window selected for each product.

## Important: expiry dates are never guessed

- OCR is optional and may misread packaging.
- LastBite only suggests a complete, valid date when the text is unambiguous.
- The user must check the package and tick the confirmation checkbox before a date affects reminder calculations.
- If the date is unclear or ambiguous (for example `03/04/25`), leave it blank.
- The prototype displays reminders inside the app when it is opened. It does not send background push notifications or SMS.

## Requirements

- Python 3.10 or newer
- A modern browser
- Optional OCR: Tesseract OCR installed on the computer, plus the Python dependencies below. Without Tesseract, image upload and manual review still work.

## Run locally (Windows / VS Code)

Open this folder in VS Code, then use **Terminal → New Terminal**.

### 1. Create and use a virtual environment

```powershell
python -m venv .venv
```

If PowerShell blocks environment activation, you can skip activation and call the venv Python directly:

```powershell
.venv\\Scripts\\python.exe -m pip install -r requirements.txt
.venv\\Scripts\\python.exe app.py
```

If activation works, use:

```powershell
.venv\\Scripts\\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

### 2. Open the app

Visit **http://127.0.0.1:5000**.

### 3. Load demo data

Click **Load demo products**. Demo records include:
- Milk with a confirmed date tomorrow
- Spinach with a confirmed date in two days
- Rice with a later confirmed date
- Yogurt with a past listed date (for demonstrating the status)
- Mystery leftovers with no expiry date (to demonstrate that LastBite does not guess)

The dates are generated relative to the day the demo data is loaded. Demo data is only for demonstrating UI behavior; real food safety depends on storage and package instructions.

## Optional OCR setup

Install Tesseract OCR for your operating system, then ensure its executable is available on PATH. Install Python packages with `pip install -r requirements.txt`. If OCR is not installed or fails, SmartScan remains usable as an upload-and-confirm workflow.

## Android demo

This project is a mobile-responsive PWA rather than a separately compiled native Android APK.

1. Run the app on a computer and connect the phone to the same Wi-Fi network.
2. Start it with `python app.py` (the app binds to `0.0.0.0`).
3. Find the computer's local network IP address and open `http://COMPUTER-IP:5000` on the Android phone. Both devices must be on the same trusted network and firewall rules may need to allow local access.
4. For a proper install prompt, deploy to HTTPS; open the deployed URL in Chrome on Android and choose **Install app** / **Add to Home screen**.

The local SQLite database lives in `data/lastbite.db`. For a multi-user production app, add authentication and per-user storage before deployment.

## Demo flow for judges

1. Click **Load demo products**.
2. Show My Fridge: milk and spinach need attention; the mystery item has no confirmed date.
3. Open SmartScan, upload a package photo, and show the review step.
4. Keep an unclear date blank, or enter the date only after reading it from the actual package and tick the confirmation box.
5. Edit a product's date and reminder window. Save and show the reminder list recalculating.
6. Remove an item to show the pantry can be maintained.

## Project structure

```text
LastBite/
├── app.py
├── requirements.txt
├── README.md
├── .gitignore
├── data/
│   └── .gitkeep
├── templates/
│   ├── base.html
│   ├── index.html
│   └── scan.html
└── static/
    ├── style.css
    ├── service-worker.js
    └── uploads/
        └── .gitkeep
```

## Current prototype limitations

- OCR depends on optional Tesseract installation and package-photo quality.
- No cloud sync, user accounts, camera-native scanning API, scheduled background push notifications, recipe assistant, shopping list or waste tracker are included.
- Data is stored locally in SQLite; it is appropriate for a hackathon demo, not a multi-user production deployment.
