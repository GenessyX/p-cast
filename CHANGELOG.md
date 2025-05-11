# CHANGELOG


## v0.1.0 (2025-05-11)

### Bug Fixes

- Stabilize stream
  ([`2a469e9`](https://github.com/GenessyX/p-cast/commit/2a469e9578ad04f8d4e0fe8e1aad84640b911900))

- Supported hls formats
  ([`c3fe64f`](https://github.com/GenessyX/p-cast/commit/c3fe64f8b5d8d313d309e9d5921f8c2e31499a38))

- Typo
  ([`83ce9bb`](https://github.com/GenessyX/p-cast/commit/83ce9bb4e1301229833e8cd3773067ad0683a92c))

### Chores

- Add classifiers
  ([`df6c30f`](https://github.com/GenessyX/p-cast/commit/df6c30fd4a9719a82a242b0dd788c0b47608a260))

- Add license
  ([`e5bf7b0`](https://github.com/GenessyX/p-cast/commit/e5bf7b0eaf473e0fd54a1fc6bdd5892d3b732277))

- Delete python-pipewire
  ([`14923e7`](https://github.com/GenessyX/p-cast/commit/14923e7d12e37c55906e31d0b8391973b593dfe0))

- Remove envs because they don't work
  ([`dec63a9`](https://github.com/GenessyX/p-cast/commit/dec63a98ce4061b6a6c095876243cc6072f4a6e3))

- Rename pipewire conf file
  ([`0d69995`](https://github.com/GenessyX/p-cast/commit/0d69995b0952642f284377262a1afd986e937aaa))

- Upd docs
  ([`03f54fc`](https://github.com/GenessyX/p-cast/commit/03f54fc7a6447f8f793326a9160d17febd51324c))

- Upd readme (delay info)
  ([`8038da1`](https://github.com/GenessyX/p-cast/commit/8038da15c2eab5befae01a40a9e65987796ce8c7))

- Update python version
  ([`7282707`](https://github.com/GenessyX/p-cast/commit/728270750b9a2f95e7bc245236b714e5898e5d24))

- Update readme
  ([`135b874`](https://github.com/GenessyX/p-cast/commit/135b87412546f489dcc622ab8b8c299a78fa5a8f))

### Features

- Add example pipewire conf to disable reconnect
  ([`5a5a177`](https://github.com/GenessyX/p-cast/commit/5a5a17757eafb25a0b35b30d5e4c61035ef07f05))

- Control audio only on chromecast device
  ([`8c53bf3`](https://github.com/GenessyX/p-cast/commit/8c53bf37175efb33d56ab503f4948c934edd8fa2))

- Get default sink from pipewire
  ([`22b6468`](https://github.com/GenessyX/p-cast/commit/22b6468a15e364f283a17972ac99c29df352c7f1))

- Initial commit
  ([`771ff85`](https://github.com/GenessyX/p-cast/commit/771ff8595a8a203157c05a7db556a4a97d0d3a25))

Starlette app with Granian server for static files serving. FFMpeg subprocess tuned for
  lower-latency. Chromecast device listening to the HLS.

- Set up main.py
  ([`f0fa486`](https://github.com/GenessyX/p-cast/commit/f0fa486966fe13c19cb2451b1abca2f61db5a656))

- Support device mute
  ([`68c8f64`](https://github.com/GenessyX/p-cast/commit/68c8f6407c0b0dad57bd7853ce2254489828d5fd))

- Update casting & app
  ([`f37e4f1`](https://github.com/GenessyX/p-cast/commit/f37e4f19bac8c75cd90238e04520b44a8ed97b53))

1. Incapsulate stream config in dataclass (add support for fmp4 segment format, libopus audiocodec).
  2. Add pause and play endpoints for chromecast control. 3. Add GZip middleware for optimization
  (chromecast supports GZip).

- Virtual device for p-cast, volume control
  ([`dff1702`](https://github.com/GenessyX/p-cast/commit/dff1702852195bfc864e3bfccc5e3616234cd17d))

1. create distinct virtual sink for p-cast. 2. pipewire-chromecast volume sync.

### Refactoring

- Organize project
  ([`4b9ab26`](https://github.com/GenessyX/p-cast/commit/4b9ab26ac6e2889fc743160f39ea7d749cd68eb9))
