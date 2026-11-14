# CHANGELOG


## v2.1.0 (2026-11-14)

### Bug Fixes

- Default virtual sink gain to unity
  ([`b7c74bb`](https://github.com/GenessyX/p-cast/commit/b7c74bb73cabef7259e1e6b179a5f8bef1b422e9))

- Keep references to background tasks to prevent a segfault
  ([`31da01b`](https://github.com/GenessyX/p-cast/commit/31da01b4c131676837318061532a77aa99131dc6))

Device add/remove/update handlers, the connection-lost handler and the cast volume handler were
  scheduled with asyncio.run_coroutine_threadsafe and the returned future was dropped. The event
  loop only keeps weak references to tasks, so such a task can be garbage collected while it is
  still running.

When that happens during SinkController.init() for a new device, the task is the only thing
  referencing the new PulseAsync and its ctypes callbacks (the controller is stored in `controllers`
  only after init()). The garbage collector frees them while libpulse still has the callbacks
  registered; the next libpulse dispatch calls a freed ctypes closure and the process segfaults:

ERROR:asyncio:Task was destroyed but it is pending! task: <Task pending name='Task-25'
  coro=<lifespan.<locals>.handle_device_add() ... python3: segfault at 48 ... in python3.13 #0
  closure_fcn #1 ffi_closure_unix64_inner #3 libpulse.so.0 #5 pa_pdispatch_run

Add p_cast/tasks.py with spawn() / spawn_threadsafe(), which keep a strong reference to each task
  until it finishes and log exceptions that were previously lost in the dropped futures. Use them
  for all fire-and-forget tasks.

- Keep sink when a device changes address
  ([`6c875bf`](https://github.com/GenessyX/p-cast/commit/6c875bf443bde3e826515fd164e04e83bcb19be1))

Speaker groups change address whenever the group leader moves to another member. p-cast treated this
  as the device going away: it removed the sink, so PipeWire moved the playing stream to another
  sink (another cast or the local speakers), and the sink only came back after a later mDNS update.

Keep the sink and only reconnect: restart the stream if the device is the active one, otherwise just
  swap in the new Chromecast object. A device that fails the health check at its new address is
  still marked unavailable as before.

- Prevent volume feedback loop while set_volume is in flight
  ([`55f7a67`](https://github.com/GenessyX/p-cast/commit/55f7a6792ed4834218d447eee671995e82cf070d))

With set_volume running in a thread, the event loop keeps processing cast status updates while the
  request waits for the device. A status sent for an earlier request then differed from
  _synced_volume (only updated after the await), so it was written to the PA sink; that sink change
  was read back as a user change and sent to the device, and the two directions kept overriding each
  other (volume bouncing between two values forever after a few quick changes).

- Update _synced_volume / _synced_mute before awaiting the cast call. - Ignore cast statuses while
  one of our own requests is in flight. - Compare PA volumes with the same epsilon as cast statuses,
  so PA's volume quantization of a cast-initiated change is not sent back to the device.

- Quit foreign receiver app before loading stream
  ([`5fddeea`](https://github.com/GenessyX/p-cast/commit/5fddeea26058e5223438b9e3424de5524e56e8c1))

A Chromecast that was last used as part of a speaker group keeps running the group follower app.
  Sending LOAD to it fails with INVALID_REQUEST, so switching from a group sink to one of its member
  sinks produced silence. Quit any app other than the backdrop or default media receiver before
  subscribing to the stream.

- Remove leftover sinks from a previous run
  ([`32fda74`](https://github.com/GenessyX/p-cast/commit/32fda740a04575144448cdd0e0dd6c696825c6a8))

If p-cast does not exit cleanly (crash, SIGKILL), its null-sink modules stay loaded. On the next
  start a second sink with the same name was created; lookups by name could return the stale sink,
  so audio was routed to a sink nobody reads from.

Before creating a sink, unload an existing module-null-sink that owns a sink with the same name. The
  instance lock guarantees no other p-cast instance is using it.

- Reset monitor state when stream activation fails
  ([`cd8b302`](https://github.com/GenessyX/p-cast/commit/cd8b302e1f9a5682e45b3e5ae17be9f30524f184))

The sink-input monitor marks a sink active before calling on_activate. When the activation was
  skipped (device unavailable) or raised, the sink stayed marked active with no stream behind it.
  Later sink-input events on the same sink did not retry the activation, so audio routed to it
  stayed silent until the user switched to another sink.

Call monitor.clear_active() on those paths, as the other failure paths already do, and only track
  the sink-input when the activation succeeded.

- Resolve the local IP on every activation
  ([`2769b85`](https://github.com/GenessyX/p-cast/commit/2769b8585d2bf28cb45d26fb144d6990c633830b))

The local IP used in the stream URL was resolved once, on the first activation, and reused for the
  lifetime of the process. After the host moved to another network or got a new address, the
  Chromecast was told to fetch the stream from the old address and stayed silent until p-cast was
  restarted. Resolve it on every activation, falling back to the previous address if the lookup
  fails.

- Restart the stream when FFmpeg exits, and drain its stderr
  ([`d4078b4`](https://github.com/GenessyX/p-cast/commit/d4078b400829801cf3a2357cef532a947186bd48))

FFmpeg was only checked for an immediate exit at startup. If it exited later (e.g. PipeWire
  restarted), nothing noticed: the HLS playlist stopped updating and, unless the Chromecast reported
  BUFFERING long enough for the watchdog, playback stayed silent. Its stderr pipe was also never
  read after startup, so enough error output could fill the pipe and block FFmpeg.

Watch the FFmpeg process for the lifetime of the stream: log its stderr lines and restart the stream
  if it exits unexpectedly.

Also compare player_state with == instead of a substring test.

- Run blocking pychromecast calls off the event loop
  ([`03e352a`](https://github.com/GenessyX/p-cast/commit/03e352a1419b3f53848a2d72743d3bbafa40dd16))

set_volume, set_volume_muted, stop, play, pause and seek wait for the device's reply (up to 10 s).
  They were called directly on the event loop, which also serves the HLS playlist and segments to
  the Chromecast. A slow reply, e.g. while changing the volume on a busy network, stalled segment
  delivery and could make the Chromecast buffer.

Run them with asyncio.to_thread.

- Serialize stream activation, deactivation and restarts
  ([`1024e57`](https://github.com/GenessyX/p-cast/commit/1024e574791b91d934189f85bb202b5dfa293bae))

on_activate, on_deactivate and restart_stream are called from the sink-input monitor, the buffering
  watchdog, the connection listener and the mDNS callbacks. An activation can wait up to ~12 s for
  the Chromecast, and meanwhile another path could start a second activation. The later one
  overwrote active_stream and left the first FFmpeg process and cast connection running.

Guard all three with an asyncio.Lock. Record the sink name in ActiveStream so a stale deactivation
  cannot tear down another sink's stream, tear down any leftover stream before activating, and skip
  a restart when the sink is no longer the active one.

- Treat IPv4-mapped IPv6 addresses as the same host
  ([`71c0103`](https://github.com/GenessyX/p-cast/commit/71c010394d19e3db85a0451ecf9cc08825926bc5))

Discovery can report a device's IPv4 address as an IPv4-mapped IPv6 address (::ffff:a0a:32b7 or
  [::ffff:a0a:32b7] for 10.10.50.183) and switch back later. This was detected as an address change
  and restarted the active stream. Normalize both addresses before comparing them.

### Chores

- Add ci, move dev dependencies to optional group
  ([`6229424`](https://github.com/GenessyX/p-cast/commit/62294242d012e1d4ee5598f294458413009d716e))

- Merge main into address-change fix
  ([`a1e161c`](https://github.com/GenessyX/p-cast/commit/a1e161caf86ef7a8ee00d1db7fdc4800ce8cebd3))

- Merge main into FFmpeg watcher fix
  ([`c04c7fa`](https://github.com/GenessyX/p-cast/commit/c04c7fa4a9cbf4dffbd0ea4c018ebd708b4edc54))

### Features

- Add --sink-gain option for the virtual sink gain
  ([`6ee9c1b`](https://github.com/GenessyX/p-cast/commit/6ee9c1bd8551c4b87207f9f9598f823b00caf8a1))

The virtual sinks apply a fixed channelmix gain of 5.0 (about +14 dB). Any source playing near full
  scale clips, which is audible as crackling on the Chromecast. Make the gain configurable so users
  can pick 1.0 (unity). The default stays 5.0 to keep the current behavior.


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
