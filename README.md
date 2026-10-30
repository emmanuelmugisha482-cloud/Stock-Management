# CYARUBARE Health Center - Stock Management System

Drug stock management for CYARUBARE Health Center: receive stock, record what is dispensed, request stock from
the main store, reconcile counts and print reports. Staff sign in with their own account.

Built with Streamlit. Data is stored in PostgreSQL (Neon). Without `DATABASE_URL` it falls back to a local
`stock.db` file, which is only meant for testing on one computer.

## Put it online (free)

1. **Neon database.** Create a free account at neon.com, make a new project, press **Connect** and copy the
   connection string (it starts with `postgresql://`). Keep it secret.
2. **GitHub.** Create a **private** repository and upload every file and folder of this project
   (`app.py`, `requirements.txt`, `README.md`, the `background` folder and the `.streamlit` folder).
3. **Streamlit Community Cloud.** Sign in at share.streamlit.io with GitHub and press **Create app**.
   Choose your repository, branch `main`, main file `app.py`. In **App URL** type a short name, for example
   `cyarubare-stock`. Open **Advanced settings** and paste your filled-in secrets into the **Secrets** box
   (see `.streamlit/secrets.toml.example`). Press **Deploy**.
4. **First login.** Use the `[first_manager]` user name and password from your secrets. Then open
   **Settings > Users** to add the dispensing users, and upload your Excel drug list in **Receive stock**.
5. **Share the address** (for example `https://cyarubare-stock.streamlit.app`). On a phone, open it and choose
   "Add to Home screen" to get an app icon.

If the connection to Neon fails and the message mentions `channel_binding`, delete `&channel_binding=require`
from the end of the connection string.

## Reports, search, Excel and clearing history

- **Reports tab** (Store manager): a daily or monthly report for the drugs that had movement (requested, main
  stock at the end of the period, received in dispensing, dispensed, dispensing stock now, who dispensed,
  who approved), downloadable as Excel. Every table has a search bar, and the usage chart and the report can
  be limited to one dispenser. Click a column heading to sort.
- **Download all data** (Reports tab): stock, usage, requests and stock movements in one Excel file. No user
  accounts are included.
- **Clear history** (Settings tab): nothing is deleted automatically. The Store manager chooses to delete
  history older than 3 months, older than 6 months, or all of it. Stock quantities, the drug list and user
  accounts are never deleted. Download an Excel copy first if you want to keep the history.

## Look and feel

Fonts and text size are set in `.streamlit/config.toml` (`font`, `headingFont`, `baseFontSize`).
Raise `baseFontSize` for bigger text everywhere. Keep this file in the `.streamlit` folder of your repository.

## Test on your own computer

    pip install -r requirements.txt
    streamlit run app.py

Put the same text in `.streamlit/secrets.toml` to test against Neon. Without it the app uses a local
`stock.db`.

## Notes

- Free Streamlit apps go to sleep after a period without visitors. The first visitor presses
  "wake up" and waits a short moment. No data is lost.
- Neon free databases also pause when idle and wake on the next use (a few seconds).
- Never put passwords in app.py or upload secrets.toml to GitHub.
