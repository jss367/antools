---
layout: page
title: Tag Index
excerpt: "An archive of posts sorted by tag."
search_omit: true
---

{% capture site_tags %}{% if site.tags %}{% for tag in site.tags %}{{ tag | first }}{% unless forloop.last %},{% endunless %}{% endfor %}{% endif %}{% endcapture %}
{% capture content_tags %}{% for doc in site.content %}{% for t in doc.tags %}{{ t }}{% unless forloop.last %},{% endunless %}{% endfor %}{% unless forloop.last %},{% endunless %}{% endfor %}{% endcapture %}
{% assign combined_tags = site_tags | append: ',' | append: content_tags %}
{% assign tags_list = combined_tags | split:',' | uniq | sort %}

<ul class="tag-box inline">
  {% for item in (0..tags_list.size) %}{% unless forloop.last %}
    {% capture this_word %}{{ tags_list[item] | strip_newlines }}{% endcapture %}
    {% assign tag_posts = site.tags[this_word] | concat: site.content | where_exp: "p", "p.tags contains this_word" %}
    <li><a href="#{{ this_word }}">{{ this_word }} <span>{{ tag_posts | size }}</span></a></li>
  {% endunless %}{% endfor %}
</ul>

{% for item in (0..tags_list.size) %}{% unless forloop.last %}
{% capture this_word %}{{ tags_list[item] | strip_newlines }}{% endcapture %}

  <h2 id="{{ this_word }}">{{ this_word }}</h2>
  <ul class="post-list">
  {% assign tag_posts = site.tags[this_word] | concat: site.content | where_exp: "p", "p.tags contains this_word" %}
  {% for post in tag_posts %}{% if post.title != null %}
    <li><a href="{{ site.url }}{{ post.url }}">{{ post.title }}</a></li>
  {% endif %}{% endfor %}
  </ul>
{% endunless %}{% endfor %}
