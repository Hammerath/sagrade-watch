name: Sagrada Ticket-Wächter

on:
  schedule:
    - cron: "*/5 * * * *"      # alle 5 Minuten (GitHub startet oft etwas verspätet)
  workflow_dispatch:            # erlaubt manuellen Start per Knopfdruck

concurrency:
  group: sagrada
  cancel-in-progress: false

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Abhängigkeiten
        run: |
          pip install playwright requests
          playwright install --with-deps chromium

      # Merkt sich, welche Slots schon gemeldet wurden, damit keine Doppel-Pushes kommen
      - name: Zustand laden
        uses: actions/cache/restore@v4
        with:
          path: sagrada_state.json
          key: sagrada-state-${{ github.run_id }}
          restore-keys: sagrada-state-

      - name: Tickets prüfen
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
        run: python sagrada_watch.py --once

      # Screenshots und Mitschnitt zum Nachsehen (unter dem Lauf bei "Artifacts")
      - name: Diagnose-Dateien sichern
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: diagnose
          path: debug/
          retention-days: 3
          if-no-files-found: ignore

      - name: Zustand speichern
        if: always()
        uses: actions/cache/save@v4
        with:
          path: sagrada_state.json
          key: sagrada-state-${{ github.run_id }}
