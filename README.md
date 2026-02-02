# README

## Requirements
- Python 3.9+
- Installed `requests` package

## Run
Example usage:

```bash
python twitter_scanario.py --user elonmusk --count 10 --out tweets.txt
```
Parameters:
- `--user` — username without `@`
- `--count` — number of tweets to extract (default: 10)
- `--out` — file to save the result (default: `tweets.txt`)

## Optional
- `--log run.log` — save execution log (enabled by default)  
  To disable the log file:
  ```bash
  --log -
  ```
- `--no-retweets` — exclude retweets  
- `--no-replies` — exclude replies
