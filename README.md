# One Piece TCG price tracker

Every day after TCGplayer prices update, a GitHub Action downloads the market
price of every One Piece Card Game single (via [TCGCSV](https://tcgcsv.com)) and saves it here.

- `data/latest.csv` — every single with today's, yesterday's and last week's price (open in any spreadsheet app)
- `data/movers/latest.json` — today's biggest risers and fallers
- `data/prices/` — one file per day, the full price history
- `data/catalog.csv` — card names, sets, numbers, rarities and TCGplayer links

The percentage lists skip cards under $2 and moves under $0.50, since cheap
cards with few sales jump around a lot. Prices are TCGplayer's market price
(recent sales), not listings.

Run it by hand from the **Actions** tab → *Daily One Piece prices* → *Run workflow*.
