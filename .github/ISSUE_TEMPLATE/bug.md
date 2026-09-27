---
name: Something else is broken
about: Installation, buttons, music and photos, network, the API
title: ""
labels: bug
---

**What you were doing, and what happened.**

**Hardware.** Which Raspberry Pi, which Zigbee adapter and buttons, which
television.

**Which components are installed?** (the installer's summary, or
`ls /etc/systemd/system/maman-*`)

**Logs.**

```
sudo journalctl -u maman-api -n 200 --no-pager
```

For the buttons, add `sudo journalctl -u zigbee2mqtt -n 100 --no-pager`.

Please check there is no password or network key in what you paste.
