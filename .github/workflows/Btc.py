name: BTC 15m Signal Bot

on:
  schedule:
    - cron: "0 */6 * * *"
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: btc-signal-bot
  cancel-in-progress: false

jobs:
  run-bot:
    runs-on: ubuntu-latest
    timeout-minutes: 355
    steps:
      - uses: actions/checkout@v4
        with:
          ref: main

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: requirements.txt

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Write Kalshi private key (optional)
        env:
          KALSHI_PRIVATE_KEY: ${{ secrets.KALSHI_PRIVATE_KEY }}
        run: |
          if [ -n "$KALSHI_PRIVATE_KEY" ]; then
            printf '%s\n' "$KALSHI_PRIVATE_KEY" > "$RUNNER_TEMP/kalshi.pem"
            chmod 600 "$RUNNER_TEMP/kalshi.pem"
            echo "KALSHI_KEY_PATH=$RUNNER_TEMP/kalshi.pem" >> "$GITHUB_ENV"
          fi

      - name: Run bot
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          KALSHI_KEY_ID: ${{ secrets.KALSHI_KEY_ID }}
          RUN_SECONDS: "20700"
          POLL_SECONDS: "1"
          RECORD: "0"
        run: python Btc.py

      - name: Save state
        if: always()
        run: |
          git config user.name "signal-bot"
          git config user.email "bot@users.noreply.github.com"
          for f in btc_state.json btc_windows.jsonl btc_calib.json; do
            [ -f "$f" ] && git add -f "$f"
          done
          if ! git diff --cached --quiet; then
            git commit -m "update btc state [skip ci]"
            git pull --rebase origin main || true
            git push origin HEAD:main
          fi
