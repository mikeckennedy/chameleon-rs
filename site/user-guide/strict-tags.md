# Catching unclosed tags

A common complaint about Chameleon is that a template mistake gets reported far from where it is. The usual cause is an unclosed tag, and chameleon-rs can catch it where it happens with one setting: `strict_tags`.


# The problem

When Chameleon finds a start tag with no matching end tag, it treats the tag as an empty element and carries on. Any `tal:`, `metal:` or `i18n:` attribute on that tag then applies to the tag alone, not to the content you meant it to cover. The template compiles without complaint, and one of two things happens.

**It renders the wrong thing, with no error.** Here the missing `</div>` means `tal:condition` hides an empty `<div>`, and the "secret" paragraph shows anyway:


``` python
from chameleon_rs import PageTemplate

page = PageTemplate("""
<section>
  <div tal:condition="user.is_admin">
    <p>Admin tools</p>
</section>
""")

class User:
    is_admin = False

print(page(user=User()))
```


    <section>
      
        <p>Admin tools</p>
    </section>


**Or it fails somewhere else.** Here the missing `</ul>` is on line 3, but the error names a variable on line 4. In a real page the symptom can be dozens of lines from the cause.


``` python
page = PageTemplate("""
<main>
  <ul tal:repeat="episode episodes">
    <li>${episode}</li>
</main>
""")

try:
    page(episodes=['Rust', 'Python'])
except NameError as e:
    print('NameError:', '\n'.join(str(e).splitlines()[:4]))
```


    NameError: episode

     - Expression: "episode"
     - Filename:   <string>


# The fix: `strict_tags`

With `strict_tags=True`, the unclosed tag is a `ParseError` when the template compiles. The error points at the tag that's missing its end tag, and says what to add:


``` python
from chameleon_rs.exc import ParseError

source = """
<main>
  <ul tal:repeat="episode episodes">
    <li>${episode}</li>
</main>
"""

try:
    PageTemplate(source, strict_tags=True)
except ParseError as e:
    print(e)
```


    Unclosed element <ul>: its tal:repeat applies to the start tag only, not to the content after it. Add the missing </ul>, or end the tag with "/>" if it is meant to be empty.

     - String:     "ul"
     - Filename:   <string>
     - Location:   (line 3: col 3)
     - Source:     <ul tal:repeat="episode episodes">
                    ^^


# Turning it on

It's off by default, because Chameleon accepts these templates and chameleon-rs matches Chameleon unless you ask otherwise. Turn it on in whichever way suits you:

- **For the whole process:** `chameleon_rs.configure(template_folder='templates', strict_tags=True)`. That covers [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) and every framework integration, including the frameworks' own [configure()](../reference/configure.md#chameleon_rs.configure) calls made afterwards.
- **Without changing code:** set the environment variable `CHAMELEON_STRICT_TAGS=1`. This is handy in development and in tests. Together with `CHAMELEON_EAGER=1`, a test that loads every template catches these mistakes before they ship.
- **Per template or loader:** `PageTemplate(source, strict_tags=True)`, `PageTemplateFile(path, strict_tags=True)` or `PageTemplateLoader(folder, strict_tags=True)`.

It's also safe to leave on in production. It only adds a check at compile time, so rendering speed doesn't change. All 229 templates of two production sites compile with it on.


# What it checks

An element is reported when it has no end tag, isn't self-closed with `/>`, and either:

- carries a `tal:`, `metal:` or `i18n:` attribute (or a `data-tal-*` one with `enable_data_attributes`), or
- is itself in one of those namespaces, such as `<tal:block>`.

These are still allowed:

- HTML's void elements, which never have an end tag: `<input tal:attributes="...">`, `<img>`, `<br>`, `<meta>`, `<link>` and the rest.
- Unclosed elements without template attributes, such as HTML's optional `</p>` and `</li>`. They render the same either way.
- `${...}` in an unclosed element's attributes. An interpolation affects only its own attribute, so nothing is lost.
