# Smart Home

Let your Wizard check and control your smart-home devices: unmodified [Home Assistant](https://www.home-assistant.io) on your box, set up for you.

A [Wizard app](https://www.aiwizards.com/wizard-apps): install it from the Wizard Apps screen, or with the Wizard App Manager on any box running stock [Runtipi](https://runtipi.io).

- `wizard.md`: what the app is and what it gives the agent (spec: wizard.md v0.2).
- `runtipi/`: stock Runtipi `config.json` and `docker-compose.yml`. The engine is the official `ghcr.io/home-assistant/home-assistant` image, pinned by digest, unchanged.
- `mcp/`: the app's front (`front.py` + `tools.py`), inlined into the compose file by `python3 mcp/inline.py`. It creates Home Assistant's owner account on first start, serves the Smart Home page at `/smart-home/` and the agent's tools at `/mcp` (`home_status`, `home_devices`, `home_device`, `home_control`; box-only), and passes everything else unchanged to Home Assistant.
- `SKILL.md`: the skill the agent loads when this app is installed.

## How it works

1. Install. The app creates Home Assistant's owner account and finishes its first-run steps itself. The password lives only in the app's private volume, and the page shows it under "Home Assistant sign-in" if you ever want the full Home Assistant app.
2. Open Smart Home and connect a brand. Xiaomi has a built-in button. When you tap it, the box downloads Xiaomi's official Home Assistant integration from Xiaomi's GitHub release (checksum-pinned) into your own Home Assistant. After you sign in, Xiaomi redirects to an address that won't load. You paste that address back into the page, which is how it works on a box that isn't on your home network. For any other brand, tap "Other brands" to open Home Assistant itself.
3. Ask your agent: "start the vacuum", "what's the air quality at home?", "turn off the living room purifier".

## Limits

- Camera video stays in the brand's app. Xiaomi encrypts camera streams end to end, so no third-party software can view them.
- Unlocking doors, disarming alarms and opening covers need the owner's explicit yes (`confirmed: true`).
- Cloud-connected devices work from a server anywhere. Zigbee, Bluetooth and local-only devices need the box on the home network.
- Third-party integrations are downloaded onto your box under their own licences. This repository contains none of them. Xiaomi's integration is licensed for personal, non-commercial use.

## License

Home Assistant is Apache-2.0. The packaging in this repo is Apache-2.0 as well.
