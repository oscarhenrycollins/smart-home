# Smart Home

Let your Wizard check and control your smart-home devices: unmodified [Home Assistant](https://www.home-assistant.io) on your box, set up for you.

A [Wizard app](https://www.aiwizards.com/wizard-apps): install it from the Wizard Apps screen, or with the Wizard App Manager on any box running stock [Runtipi](https://runtipi.io).

- `wizard.md`: what the app is and what it gives the agent (spec: wizard.md v0.2).
- `runtipi/`: stock Runtipi `config.json` and `docker-compose.yml`. The engine is the official `ghcr.io/home-assistant/home-assistant` image, pinned by digest, unchanged.
- `mcp/`: the app's front (`front.py` + `tools.py`), inlined into the compose file by `python3 mcp/inline.py`. It creates Home Assistant's owner account on first start, serves the Smart Home page at `/smart-home/` and the agent's tools at `/mcp` (`home_status`, `home_devices`, `home_device`, `home_control`; box-only), and passes everything else unchanged to Home Assistant.
- `SKILL.md`: the skill the agent loads when this app is installed.

## How it works

1. Install. The app creates Home Assistant's owner account and finishes its first-run steps itself. The password lives only in the app's private volume, and the page shows it under "Home Assistant sign-in" if you ever want the full Home Assistant app.
2. Open Smart Home and connect your devices' brand. Some brands have a sign-in button on the page: tapping it fetches that brand's own Home Assistant integration from the brand's official release (checksum-pinned) onto your box, then you sign in and paste back the address the sign-in ends on (it works from a server that isn't on your home network). Any other brand: tap "Other brands" to open Home Assistant itself.
3. Ask your agent: "start the vacuum", "what's the air quality at home?", "turn off the living room purifier".

## Limits

- Camera video stays in the brand's app when the brand encrypts its streams end to end.
- Unlocking doors, disarming alarms and opening covers need the owner's explicit yes (`confirmed: true`).
- Cloud-connected devices work from a server anywhere. Zigbee, Bluetooth and local-only devices need the box on the home network.
- Brand integrations are downloaded onto your box under their own licences, for your own home use. This repository contains none of them.

## License

Home Assistant is Apache-2.0. The packaging in this repo is Apache-2.0 as well.
