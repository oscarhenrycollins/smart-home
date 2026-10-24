---
wizard: 0.2
id: smart-home
name: Smart Home
type: service
category: app
tags: [automation]
version: 0.1.2
summary: Let your Wizard check and control your smart-home devices, like purifiers, vacuums, lights and plugs.
icon: icon.png
license: Apache-2.0
source: https://github.com/oscarhenrycollins/smart-home
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
mcp:
  path: /mcp
  port: 8080
  transport: streamable-http
skill: SKILL.md
provides:
- smart-home
health: /smart-home/healthz
view:
  path: /smart-home/
  port: 8080
---

# Smart Home

Your Wizard's link to the devices in your home: unmodified [Home Assistant](https://www.home-assistant.io) on your box, set up for you.

- **You** open Smart Home, sign in to your device brand once, and see every device by room with on/off switches.
- **Your agent** checks and controls devices for you: "start the vacuum", "is the air bad at home?", "put the purifier in sleep mode".
- **Any brand Home Assistant supports** can be added from the app's "Other brands" link.

Camera video stays in the brand's own app; agents can switch cameras on, off or into privacy mode only. Unlocking doors or disarming alarms always needs your yes.
