---
layout: page
title: Pages
excerpt: "An archive of pages."
search_omit: true
---

<ul class="post-list">
{% for post in site.content %} 
  <li><article><a href="{{ site.url }}{{ post.url }}">{{ post.title }}{% if post.excerpt %} <span class="excerpt">{{ post.excerpt | remove: '\\[ ... \\' | remove: '\\( ... \\' | markdownify | strip_html | strip_newlines | escape_once }}</span>{% endif %}</a></article></li>
{% endfor %}
</ul>
