const stage = document.querySelector(".dispatch-stage");
const replay = document.getElementById("replay-dispatch");
const copy = document.getElementById("dispatch-copy");
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
const visualQaMode = navigator.webdriver === true;

if (stage) {
  const poster = "./assets/pipeline-bus-parked.png";
  const animation = "./assets/pipeline-bus-clawd-boarding.gif";
  const media = document.createElement("img");
  media.className = "dispatch-bus-media";
  media.src = poster;
  media.alt = "Hand-drawn pixel animation of Clawd boarding the Pipeline Bus";
  media.width = 384;
  media.height = 256;
  media.decoding = "sync";
  stage.append(media);

  const playOriginalGif = () => {
    if (reducedMotion.matches) {
      media.src = poster;
      return;
    }

    copy.textContent = "Doors open. Clawd is boarding now.";
    media.src = poster;
    requestAnimationFrame(() => {
      media.src = `${animation}?run=${Date.now()}`;
    });

    window.setTimeout(() => {
      copy.textContent = "The task has departed for Implementation.";
    }, 6200);
  };

  replay?.addEventListener("click", playOriginalGif);
  if (!reducedMotion.matches && !visualQaMode) {
    window.setTimeout(playOriginalGif, 650);
  }
  reducedMotion.addEventListener?.("change", () => {
    if (reducedMotion.matches) media.src = poster;
  });
}
