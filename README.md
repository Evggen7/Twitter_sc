# README

## Requirements
- Python 3.9+
- Installed `requests`
- Installed `beautifulsoup4`

Install dependencies:

```bash
pip install requests beautifulsoup4
```
## Run
Example usage:

```bash
python twitter_scanario.py --user elonmusk --count 10 --out tweets.txt
```
Parameters:
- `--user` — username without `@`
- `--count` — number of tweets to extract (default: 10)
- `--out` — file to save the result (default: `tweets.txt`)
