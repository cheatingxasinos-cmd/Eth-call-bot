
name: BTC 15m Signal Bot

on:
  schedule:
    - cron: "0 */6 * * *"   # new run every 6h; each run lasts ~5h45m so runs hand off
  workflow_dispatch:

permissions:
  contents: write            # needed to commit the state files back

concurrency:
  group: btc-signal-bot
  cancel-in-progress: false  # let the running job finish; the next one queues

jobs:
  run-bot:
    runs-on: ubuntu-latest
    timeout-minutes: 355     # hard cap, under the 360 min GitHub limit
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
          RUN_SECONDS: "20700"   # 345 min, then the script exits cleanly
          POLL_SECONDS: "1"
          RECORD: "0"            # skip the large btc_ticks.jsonl tick recorder
        run: python Btc.py

      - name: Save state
        if: always()
        run: |
          git config user.name "signal-bot"
          git config user.email "bot@users.noreply.github.com"
          git add -f btc_state.json btc_windows.jsonl btc_calib.json 2>/dev/null || true
          if ! git diff --cached --quiet; then
            git commit -m "update btc state [skip ci]"
            git pull --rebase origin main || true
            git push origin HEAD:main
          fi
