---
layout: post
title: Navigating a Human World
excerpt: "All the things needed to navigate a human-shaped world. Using public transit, picking up trash, crossing the street, etc."
categories: pages
tags: [Pigeon, Deer, Crow, Dog]
image:
  feature: feature.jpg
comments: true
share: true
title: Pigeon Public Transit
id: eaa5b485645344809f9225326d20dac8
comment: "Evidence for theory of mind?"
- kind: video
title: Deer Crossing The Street
id: c0f5977434994706bac6a7c84f66e0c5
comment: "Evidence for theory of mind?"
- kind: video
title: Dog Moving Fence
id: c1c5216e10ce4266bce8b6ae77aa8861
comment: "Evidence for theory of mind?"
title: Crow Picking Up Trash
id: 466f6fde163748d2a7365481193b19a4
comment: "Evidence for theory of mind?"
- kind: video
title: Pigeon Using Public Transportatio
id: 534f12b5418f44f5a6324ba65793f178
comment: "Evidence for theory of mind?"
- kind: video
title: Dog With Stick Through Doors
id: 3976a5d641294c93a8342e9da6e76e01
comment: "Evidence for theory of mind?"
- kind: video
title: Dog Waiting For Traffic Signal
id: 2edbe9634f29452fa57ade2780d0c114
comment: "Evidence for theory of mind?"
---

---

## Pigeon Using Public Transportation

> Note: Sped up 2X to reduce size???

<img src='https://github.com/jss367/antools/blob/gh-pages-2.3.4/assets/images/human_tools/pigeon_using_public_transportation.gif?raw=true' />

## Deer Crossing the Street

> Note: Sped up 3X to reduce size

<img src='https://github.com/jss367/antools/blob/gh-pages-2.3.4/assets/images/human_tools/deer_crossing_the_street.gif?raw=true' />

## Crow Picking up Trash

> Note: Sped up 5X to reduce size

<img src='https://github.com/jss367/antools/blob/gh-pages-2.3.4/assets/images/human_tools/crow_picking_up_trash.gif?raw=true' />

## Dog Waiting for Traffic Signal

> Note: Sped up 2X to reduce size

<img src='https://github.com/jss367/antools/blob/gh-pages-2.3.4/assets/images/human_tools/dog_waiting_for_traffic_signal.gif?raw=true' />

## Dog moving fence

<iframe
  class="autoplay-video"
  src="https://customer-1ixj2hastb04w2ye.cloudflarestream.com/ea1488771affd3099d681856312bed34/iframe?muted=true&autoplay=false"
  style="width:100%;aspect-ratio:16/9;border:0"
  allow="accelerometer; autoplay; encrypted-media; picture-in-picture"
  allowfullscreen>
</iframe>

## Dog picking up toy

<iframe
  class="autoplay-video"
  src="https://customer-1ixj2hastb04w2ye.cloudflarestream.com/ba3c3f73c4ff8cba079687cbd085ab3c/iframe?muted=true&autoplay=false"
  style="width:100%;aspect-ratio:16/9;border:0"
  allow="accelerometer; autoplay; encrypted-media; picture-in-picture"
  allowfullscreen>
</iframe>

## Cat making bed

<iframe
  class="autoplay-video"
  src="https://customer-1ixj2hastb04w2ye.cloudflarestream.com/f44ec86400e9258921edf15916d17c09/iframe?muted=true&autoplay=false"
  style="width:100%;aspect-ratio:16/9;border:0"
  allow="accelerometer; autoplay; encrypted-media; picture-in-picture"
  allowfullscreen>
</iframe>

## Cow undoing cage

<iframe
  class="autoplay-video"
  src="https://customer-1ixj2hastb04w2ye.cloudflarestream.com/ba98a7130545eba92700fadee37118b8/iframe?muted=true&autoplay=false"
  style="width:100%;aspect-ratio:16/9;border:0"
  allow="accelerometer; autoplay; encrypted-media; picture-in-picture"
  allowfullscreen>
</iframe>

<script>
document.addEventListener("DOMContentLoaded", function () {
  const iframes = document.querySelectorAll(".autoplay-video");
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      const iframe = entry.target;
      const player = iframe.contentWindow;
      if (entry.isIntersecting) {
        player.postMessage({ event: "play" }, "*");
      } else {
        player.postMessage({ event: "pause" }, "*");
      }
    });
  }, { threshold: 0.5 });

  iframes.forEach(iframe => observer.observe(iframe));
});
</script>
