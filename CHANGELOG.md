# CHANGELOG


## v2.0.0 (2026-03-01)

### Bug Fixes

- Disconnect idle chromecasts, fix failed-device handling and duplicate sinks. didn't know
  pychromecast maintains a persistent TCP connection with PING/PONG heartbeats from the moment we
  wait on a Chromecast object
  ([`5522a9a`](https://github.com/GenessyX/p-cast/commit/5522a9a844bc42583881cd6d52dbc02528386e60))

- Gracefully handle multiple SIGTERMs at once
  ([`42f2ed5`](https://github.com/GenessyX/p-cast/commit/42f2ed53dd74c55a529c51378c7d7c954d0b8722))

- Ignore benign race where two CCs are simultaneously initializing
  ([`143a79e`](https://github.com/GenessyX/p-cast/commit/143a79e356d1f0b427e412161ba8d3df2531310d))

- Once detached from terminal, sigterm wasn't being handled
  ([`a9b78dc`](https://github.com/GenessyX/p-cast/commit/a9b78dca53a9fda980b50e8c758562d4265d1d91))

so graceful exit wasn't possible. Now we have explicit sigterm handling. This also greatly speeds up
  clean shutdown. Finally, we now also tell CC we're quitting if there's an active stream, rather
  than just killing ffmpeg and httpd and letting it fail.

- Regression and various minor issues
  ([`3db7076`](https://github.com/GenessyX/p-cast/commit/3db7076dd113928b2bd514080461e84e6acb8fa7))

Kindly telling CC to quit_app() was a regression; this causes it to disconnect from US triggering
  recovery attempts -- use stop() instead. Don't wait for chromecast.disconnect() to complete. Avoid
  race when handle_device_add is called in rapid succession. Register BufferingWatchdog before
  subscribing, so we can log load_media_failed messages. Don't exit if we don't immediately find
  chromecasts, we will find them later. Defer finding IP address until we actually do find some (so
  we have network).

### Chores

- Merge commit 'e4cee86f' into multi-sink
  ([`8b838fb`](https://github.com/GenessyX/p-cast/commit/8b838fb08c6865e5abee85bd2e52a85646d31a46))

and make required pre-commit changes. No functional changes

- Minor refactoring
  ([`7b35765`](https://github.com/GenessyX/p-cast/commit/7b357657efa62bc1e17805331dd22089136443f3))

- Set up & configure linters
  ([`e4cee86`](https://github.com/GenessyX/p-cast/commit/e4cee86fadd5297e5212715225d487c23a523d0d))

- Simplify handle_device_add and always check availability
  ([`0bb5988`](https://github.com/GenessyX/p-cast/commit/0bb5988bbf60546ea6cb340f4ae82ab0a6cbcdc0))

- Update readme
  ([`d7b985d`](https://github.com/GenessyX/p-cast/commit/d7b985d623b58615c51996627b20fa49584a816b))

- Update readme and add excalidraw diagram
  ([`1da3e79`](https://github.com/GenessyX/p-cast/commit/1da3e793e034f5d615d9ce0f5206df2a238fbbd6))

- Update readme and pyproject
  ([`9d5ad51`](https://github.com/GenessyX/p-cast/commit/9d5ad5146e299f58db5e2d0218a55a76ce8c084c))

### Features

- Add ability to run in background
  ([`2f129ec`](https://github.com/GenessyX/p-cast/commit/2f129ec6daabb28b97010b6cdbc3428ebd6df325))

via explicit double-forking. Using OS-level backgrounding caused the pychromecast thread to stall.
  Also acquire an instance lock to ensure there are not multiple p-casts running simultaneously.
  Adds systemd service file to enable running as a service.

- Better ffmpeg error handling
  ([`a6f3e58`](https://github.com/GenessyX/p-cast/commit/a6f3e58fa96c11e6173d5c1d3eaa7b0ed3e001d3))

Exit upon immediate ffmpeg exit: that indicates ffmpeg or configuration error or programming
  mistake. Remove ffmpeg_watcher. Now that we monitor media events, it's redundant.

- Better resilience to network errors
  ([`efdca40`](https://github.com/GenessyX/p-cast/commit/efdca408a253ef434eeaed2dc20eba0818312c54))

- handle device updates including changed device IPs/ports or newly-re-available devices that never
  left mDNS. now more responsive to devices reappearing. - cleanly handle benign race condition
  where temp dir is cleaned up while http requests are still incoming - include name of sending
  computer with stream - improved logging

- Handle cc device failure. time out quickly if tcp connection to device fails. then remove sink &
  don't re-add it from zeroconf until tcp probe succeeds (zeroconf ttl is ~75m)
  ([`7f13976`](https://github.com/GenessyX/p-cast/commit/7f139765a5c6492ccda10a0a931092eaf0b23835))

- Monitor for cc buffering and recover; add input ffmpeg queue
  ([`49d5246`](https://github.com/GenessyX/p-cast/commit/49d5246cd804f6ad65dee1a56c6aad674e11baf7))

- Only resample if above chromecast maximum. use friendly sink name. add command line arguments. tcp
  port configurable.
  ([`30fc774`](https://github.com/GenessyX/p-cast/commit/30fc774d0a97b58c758be1cd624e8186ea906206))

- Remove/don't re-add sinks for unresponsive chromecasts in a timely manner
  ([`29967e6`](https://github.com/GenessyX/p-cast/commit/29967e6b805765dab680c9f68fe713d42cb938a8))

- Support logging to file. minor simplification.
  ([`1af7b14`](https://github.com/GenessyX/p-cast/commit/1af7b146c39bf5cdb8238b0126a48d42c6496cd6))

- Sync CC-initiated volume changes
  ([`cf71b08`](https://github.com/GenessyX/p-cast/commit/cf71b087051c51f403a42c51a6358041dde850ba))

Monitor CC volume-change events, keep internal shared volume in SinkController. Now volume control
  is seamless in both directions.

- Update_service can also indicate unresponsive cast devices
  ([`1982906`](https://github.com/GenessyX/p-cast/commit/1982906e405d69a4fc0ea59d88b120a2864af0ce))

So just use the handle_device_add to handle both add and updates, but update to handle change of
  address case.

- Use systemd type=notify, as recommended for services, not forking
  ([`2fff7b2`](https://github.com/GenessyX/p-cast/commit/2fff7b221402e65b9d6cecaca94341423110334d))

systemd was floundering at PID file not being written when parent process ends with --daemonize, and
  also didn't like it being overwritten


## v1.1.0 (2025-05-11)

### Chores

- Update readme with uvx, pipx
  ([`f0ff0ac`](https://github.com/GenessyX/p-cast/commit/f0ff0ac432128264b991936e0a4c91aea0bdf657))

### Features

- Set up project.urls
  ([`1d7955d`](https://github.com/GenessyX/p-cast/commit/1d7955ddef74c5f792f62ed06cb3c2efe0fd4341))


## v1.0.0 (2025-05-11)

### Chores

- Set up semantic-release
  ([`7a6cd44`](https://github.com/GenessyX/p-cast/commit/7a6cd440d0a80694066d770ccf82e3de8cb3dd15))


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
