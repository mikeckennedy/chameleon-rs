# Email bodies

An email needs an HTML body and a plain-text one. Render both from templates, with [PageTemplate](../reference/PageTemplate.md#chameleon_rs.PageTemplate) for HTML and [PageTextTemplate](../reference/PageTextTemplate.md#chameleon_rs.PageTextTemplate) for text. A text template uses the same `${...}` expressions but doesn't escape HTML or parse tags.


``` python
from chameleon_rs import PageTemplate, PageTextTemplate

html_body = PageTemplate("""\
<p>Hi ${user},</p>
<p>Your order <strong>#${order.id}</strong> has shipped:</p>
<ul><li tal:repeat="item order.items">${item}</li></ul>
""")

text_body = PageTextTemplate("""\
Hi ${user},

Your order #${order.id} has shipped:
${'\\n'.join('- ' + item for item in order.items)}
""")


class Order:
    id = 1042
    items = ['Rust & Python mug', 'Sticker <3']


print(html_body(user='Ada', order=Order()))
print(text_body(user='Ada', order=Order()))
```


    <p>Hi Ada,</p>
    <p>Your order <strong>#1042</strong> has shipped:</p>
    <ul><li>Rust & Python mug</li>
    <li>Sticker <3</li></ul>

    Hi Ada,

    Your order #1042 has shipped:
    - Rust & Python mug
    - Sticker <3


The HTML body escapes `&` and `<`, and the text body leaves them alone.

In an app, keep the email templates in your template folder (for example `emails/`), and load them with [PageTemplateLoader](../reference/PageTemplateLoader.md#chameleon_rs.PageTemplateLoader), or [PageTextTemplateFile](../reference/PageTextTemplateFile.md#chameleon_rs.PageTextTemplateFile) for the text version. Emails can use partials too. [render_partial](../reference/render_partial.md#chameleon_rs.render_partial) works in any template, so the footer you share with the site can live in one file:

``` html
<p>Hi ${user},</p>
...
${render_partial('emails/_footer.pt', unsubscribe_url=unsubscribe_url)}
```

Then pass the two strings to your mail library, such as `email.message.EmailMessage`'s `set_content()` and `add_alternative(..., subtype='html')`.
