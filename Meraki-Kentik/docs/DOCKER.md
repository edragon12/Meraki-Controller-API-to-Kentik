# Running with Docker

The collector has no third-party Python dependencies. Credentials stay in a local `.env` file and are never baked into the image.

```bash
cp .env.example .env
chmod 600 .env
nano .env

docker build -t meraki-kentik .
docker run --rm --env-file .env meraki-kentik python collector.py --mode real --dry-run
docker run -d --restart unless-stopped --name meraki-kentik --env-file .env meraki-kentik
```

Useful commands:

```bash
docker logs -f meraki-kentik     # follow collection output
docker stop meraki-kentik        # stop
docker rm meraki-kentik          # remove before re-running with a new image
```

The default container command collects every 300 seconds. Docker's `--env-file` expects plain `KEY=VALUE` lines without quotes.
