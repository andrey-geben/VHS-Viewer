/* VHS WHEP client: прямое WebRTC-подключение к MediaMTX с Basic Auth. */
(function () {
  function base64Utf8(value) {
    const bytes = new TextEncoder().encode(value);
    let binary = "";
    bytes.forEach((b) => { binary += String.fromCharCode(b); });
    return btoa(binary);
  }

  function waitIce(pc, timeoutMs = 1800) {
    return new Promise((resolve) => {
      if (pc.iceGatheringState === "complete") return resolve();
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        pc.removeEventListener("icegatheringstatechange", check);
        resolve();
      };
      const check = () => { if (pc.iceGatheringState === "complete") finish(); };
      const timer = setTimeout(finish, timeoutMs);
      pc.addEventListener("icegatheringstatechange", check);
    });
  }

  async function connect(video, streamUrl, options = {}) {
    const parsed = new URL(streamUrl, window.location.href);
    const user = decodeURIComponent(parsed.username || "");
    const pass = decodeURIComponent(parsed.password || "");
    const path = parsed.pathname.endsWith("/") ? parsed.pathname : parsed.pathname + "/";
    const whepUrl = new URL("whep", parsed.origin + path).toString();

    const pc = new RTCPeerConnection({ iceServers: [] });
    let closed = false;
    const handle = {
      pc,
      close() {
        closed = true;
        try { pc.close(); } catch (e) {}
        try { video.srcObject = null; } catch (e) {}
      },
    };

    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    pc.ontrack = (event) => {
      if (closed) return;
      video.srcObject = event.streams[0] || new MediaStream([event.track]);
      video.play().catch(() => {});
      options.onState && options.onState("playing");
    };
    pc.onconnectionstatechange = () => {
      if (closed) return;
      const state = pc.connectionState;
      options.onState && options.onState(state);
      if (state === "failed" || state === "disconnected") {
        try { pc.close(); } catch (e) {}
      }
    };

    const offer = await pc.createOffer();
    offer.sdp = offer.sdp || "";
    await pc.setLocalDescription(offer);
    await waitIce(pc);

    const headers = { "Content-Type": "application/sdp" };
    if (user || pass) headers.Authorization = "Basic " + base64Utf8(user + ":" + pass);

    // Сигналинг идёт через свой backend: браузер не делает cross-origin
    // запрос к MediaMTX, поэтому CORS и Basic Auth не мешают.
    const proxyHeaders = { "Content-Type": "application/json" };
    if (window.CSRF_TOKEN) proxyHeaders["X-CSRF-Token"] = window.CSRF_TOKEN;
    const response = await fetch("/api/whep", {
      method: "POST",
      headers: proxyHeaders,
      body: JSON.stringify({ url: streamUrl, sdp: offer.sdp }),
    });
    let payload = {};
    try { payload = await response.json(); } catch (e) {}
    if (!response.ok || !payload.ok) {
      try { pc.close(); } catch (e) {}
      throw new Error(payload.error || ("WHEP HTTP " + response.status));
    }
    const answer = payload.sdp || "";
    if (!answer.trim()) {
      try { pc.close(); } catch (e) {}
      throw new Error("WHEP: пустой ответ");
    }
    await pc.setRemoteDescription({ type: "answer", sdp: answer });
    options.onState && options.onState("connected");
    return handle;
  }

  window.VHSWhep = { connect };
})();
