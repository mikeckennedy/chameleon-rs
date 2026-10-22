# A shared layout

Most sites have one layout: the `<head>`, the navigation and the footer. Write it once as a METAL macro with slots, and have each page fill them.

The layout, `shared/_layout.pt` (the underscore marks it as not a page of its own):

``` html
<!DOCTYPE html>
<html lang="en" metal:define-macro="layout">
<head>
  <title><tal:title metal:define-slot="title">My site</tal:title></title>
  <link rel="stylesheet" href="/static/site.css">
  <tal:head metal:define-slot="head"></tal:head>
</head>
<body>
  <nav>[Home](/) <span tal:condition="user">Hi, ${user}</span></nav>
  <main metal:define-slot="content"></main>
  <tal:scripts metal:define-slot="scripts"></tal:scripts>
</body>
</html>
```

A page, `home/index.pt`, loads the layout relative to itself with `load:` and fills the slots it needs:

``` html
<html metal:use-macro="load: ../shared/_layout.pt">
  <tal:title metal:fill-slot="title">Home | My site</tal:title>
  <main metal:fill-slot="content">
    <h1>Welcome</h1>
  </main>
  <script metal:fill-slot="scripts" src="/static/home.js"></script>
</html>
```


``` python
from chameleon_rs import PageTemplateLoader

templates = PageTemplateLoader([template_folder])
print(templates['home/index.pt'](user='Ada'))
```


    <!DOCTYPE html>
    <html lang="en">
    <head>
      <title>Home | My site</title>
      <link rel="stylesheet" href="/static/site.css">
      
    </head>
    <body>
      <nav>[Home](/) <span>Hi, Ada</span></nav>
      <main>
        <h1>Welcome</h1>
      </main>
      <script src="/static/home.js"></script>
    </body>
    </html>


A few things to note:

- A slot the page doesn't fill keeps the layout's default content: `head` stays empty here.
- The layout sees the page's variables (`user` here), so pass layout data such as the signed-in user in every view's model, or add it in one place in your framework.
- `tal:` and `metal:` elements (`<tal:title>`) render only their content. They're handy for slots that shouldn't add a tag.
- The layout is compiled once and cached by the loader, like any template.
