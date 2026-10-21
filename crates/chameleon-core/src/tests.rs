//! End-to-end core tests with a toy expression compiler and runtime. They cover control flow
//! (D2): text coalescing, attributes, content defaults, conditions, loop whitespace and
//! restore, frames and suspension. Value conversion is tested through Python (D13).

use std::collections::HashMap;
use std::sync::Arc;

use crate::{
    Active, Attrs, Call, Countdown, DefaultAlias, Done, Emitted, Errors, Escape, Eval, ExprCompileError, ExprCompiler,
    ExprContext, ExprId, Fill, I18n, Loops, MacroStep, Macros, NameId, NameValue, Names, Options, Output, Program,
    Stage, Step, Tok, Translate, Translation, Values, Visibility, VmError, VmState, compile, escape_into, run,
};

#[derive(Clone, Debug, PartialEq)]
enum V {
    None,
    Int(i64),
    Str(String),
    List(Vec<V>),
    Default,
    Bool(bool),
    /// An ordered mapping (`dict(a=1,b='x')`).
    Dict(Vec<(String, V)>),
    /// A loop iterator: the items and an id (the position lives in the scope).
    Iter(Vec<V>, usize),
    /// A frame's code: index into the toy's programs.
    Code(usize),
    /// A frame's scope: index into the toy's scopes.
    Scope(usize),
    /// A macro: program index and entry.
    Macro(usize, u32),
    /// A slot fill: index into the toy's fills.
    Fill(usize),
}

/// Toy expressions: `none`, `true`/`false`, integers, `'strings'`, `[a, b]` lists of those,
/// `dict(a=1,b='x')`, `default` (when aliased), `attrs` (when aliased: the attribute names),
/// `call:N` (run program N as a frame), `wait:X` (suspend, resume with X's value),
/// `macro:N:name` (macro `name` of program N; `macro:N` for its main function), and names.
#[derive(Clone, Debug)]
struct Expr {
    src: String,
    default: DefaultAlias,
    attrs: Option<Vec<(String, String)>>,
    emitted: bool,
}

#[derive(Default)]
struct Toy {
    exprs: Vec<Expr>,
}

impl ExprCompiler for Toy {
    type Error = String;
    fn compile(&mut self, source: &Tok, cx: ExprContext<'_>) -> Result<ExprId, ExprCompileError<String>> {
        let s = source.text.trim();
        if s.is_empty() || s.contains('}') || s.matches('\'').count() % 2 == 1 {
            return Err(ExprCompileError::Invalid { message: format!("invalid: {s:?}"), token: None });
        }
        if s == "boom" {
            return Err(ExprCompileError::Other("boom".into()));
        }
        self.exprs.push(Expr {
            src: s.to_owned(),
            default: cx.default,
            attrs: cx.attrs.map(|a| a.items.to_vec()),
            emitted: cx.emitted,
        });
        Ok(ExprId(u32::try_from(self.exprs.len() - 1).unwrap()))
    }

    fn compile_code(&mut self, _: &Tok) -> Result<ExprId, ExprCompileError<String>> {
        Err(ExprCompileError::Other("code blocks are not supported by the toy".into()))
    }

    fn deferred(&mut self, message: &str, token: &Tok) -> Result<ExprId, ExprCompileError<String>> {
        self.exprs.push(Expr {
            src: format!("raise:{message} @ {:?}", token.text),
            default: DefaultAlias::Name,
            attrs: None,
            emitted: false,
        });
        Ok(ExprId(u32::try_from(self.exprs.len() - 1).unwrap()))
    }
}

struct World {
    programs: Vec<(Arc<Program>, Vec<Expr>)>,
    scopes: Vec<HashMap<String, V>>,
    code: usize,
    scope: usize,
    log: Vec<String>,
    iterators: usize,
    /// `rcontext`, for global definitions and loops.
    rcontext: HashMap<String, V>,
    fills: Vec<Fill<V>>,
    /// The active frame's i18n settings, as the toy's translations show them.
    i18n: String,
    /// The serial of each activated frame, in order.
    serials: Vec<u64>,
}

impl World {
    fn names(&self) -> &[Box<str>] {
        self.programs[self.code].0.names()
    }

    fn name(&self, n: NameId) -> String {
        self.names()[n.0 as usize].to_string()
    }

    fn value(&mut self, s: &str, cx: &Expr) -> Result<Step<V>, String> {
        if let Some(rest) = s.strip_prefix("wait:") {
            return Ok(Step::Suspend(self.plain(rest, cx)?));
        }
        if let Some(rest) = s.strip_prefix("call:") {
            assert!(cx.emitted, "frame calls only where emitted");
            let code: usize = rest.parse().map_err(|_| "bad call")?;
            self.scopes.push(HashMap::from([("x".to_owned(), V::Str(format!("from {code}")))]));
            let scope = self.scopes.len() - 1;
            return Ok(Step::Call(Call {
                program: Arc::clone(&self.programs[code].0),
                code: V::Code(code),
                scope: V::Scope(scope),
            }));
        }
        Ok(Step::Ready(self.plain(s, cx)?))
    }

    fn plain(&self, s: &str, cx: &Expr) -> Result<V, String> {
        if let Some(message) = s.strip_prefix("raise:") {
            return Err(format!("ExpressionError: {message}"));
        }
        if s == "interrupt" {
            return Err("KeyboardInterrupt".into());
        }
        if let Some(rest) = s.strip_prefix("macro:") {
            let (program, name) = rest.split_once(':').map_or((rest, None), |(p, n)| (p, Some(n)));
            let program: usize = program.parse().map_err(|_| "bad macro")?;
            let entry = match name {
                None => 0,
                Some(n) => self.programs[program]
                    .0
                    .macros()
                    .iter()
                    .find(|m| &*m.name == n)
                    .map(|m| m.entry)
                    .ok_or_else(|| format!("KeyError: {n}"))?,
            };
            return Ok(V::Macro(program, entry));
        }
        Ok(if s == "none" {
            V::None
        } else if s == "true" || s == "false" {
            V::Bool(s == "true")
        } else if s == "default" && cx.default == DefaultAlias::Marker {
            V::Default
        } else if s == "attrs" && cx.attrs.is_some() {
            let names: Vec<_> = cx.attrs.iter().flatten().map(|(k, v)| format!("{k}={v}")).collect();
            V::Str(names.join(","))
        } else if let Some(inner) = s.strip_prefix("dict(").and_then(|r| r.strip_suffix(')')) {
            V::Dict(
                inner
                    .split(',')
                    .filter(|p| !p.trim().is_empty())
                    .map(|p| {
                        let (k, v) = p.split_once('=').ok_or("bad dict")?;
                        Ok((k.trim().to_owned(), self.plain(v.trim(), cx)?))
                    })
                    .collect::<Result<_, String>>()?,
            )
        } else if let Ok(n) = s.parse() {
            V::Int(n)
        } else if let Some(inner) = s.strip_prefix('\'').and_then(|r| r.strip_suffix('\'')) {
            V::Str(inner.to_owned())
        } else if let Some(inner) = s.strip_prefix('[').and_then(|r| r.strip_suffix(']')) {
            V::List(
                inner
                    .split(',')
                    .filter(|p| !p.trim().is_empty())
                    .map(|p| self.plain(p.trim(), cx))
                    .collect::<Result<_, _>>()?,
            )
        } else {
            self.scopes[self.scope].get(s).cloned().ok_or_else(|| format!("NameError: {s}"))?
        })
    }
}

impl Values for World {
    type Value = V;
    type Error = String;

    fn clone_value(&mut self, value: &V) -> V {
        value.clone()
    }
}

fn i18n_label(i18n: &I18n<V>) -> String {
    format!("{:?}/{:?}/{:?}", i18n.domain.as_deref(), i18n.context.as_deref(), i18n.target_language)
}

impl Eval for World {
    fn activate(&mut self, frame: Active<'_, V>) -> Result<(), String> {
        let (V::Code(c), V::Scope(s)) = (frame.code, frame.scope) else { return Err("bad frame".into()) };
        self.code = *c;
        self.scope = *s;
        self.i18n = i18n_label(frame.i18n);
        self.serials.push(frame.serial);
        Ok(())
    }

    fn eval(&mut self, expr: ExprId, emitted: bool) -> Result<Step<V>, String> {
        let e = self.programs[self.code].1[expr.0 as usize].clone();
        assert!(e.emitted == emitted || e.src.starts_with("raise:"), "{}", e.src);
        self.value(&e.src.clone(), &e)
    }

    fn emit(&mut self, value: V, esc: Escape, stage: Stage, out: &mut Output) -> Result<Emitted, String> {
        if stage == Stage::Bool {
            // The toy's boolean conversion: an empty string is false but written.
            return Ok(match value {
                V::None | V::Default => Emitted::Nothing,
                V::Str(s) if s.is_empty() => Emitted::Value,
                _ => Emitted::True,
            });
        }
        let s = match value {
            V::None | V::Default => return Ok(Emitted::Nothing),
            V::Int(n) => n.to_string(),
            V::Str(s) => s,
            V::Bool(b) => if b { "True" } else { "False" }.to_owned(),
            other => format!("{other:?}"),
        };
        escape_into(s.as_bytes(), esc, out);
        Ok(Emitted::Value)
    }

    fn is_default(&mut self, value: &V) -> bool {
        *value == V::Default
    }

    fn truthy(&mut self, value: &V) -> Result<bool, String> {
        Ok(match value {
            V::None | V::Int(0) | V::Bool(false) => false,
            V::List(l) => !l.is_empty(),
            V::Str(s) => !s.is_empty(),
            _ => true,
        })
    }

    fn is_false(&mut self, value: &V) -> bool {
        *value == V::Bool(false)
    }
}

impl Names for World {
    fn lookup(&mut self, name: NameId) -> Result<Option<V>, String> {
        let n = self.name(name);
        self.log.push(format!("backup {n}"));
        Ok(self.scopes[self.scope].get(&n).cloned())
    }

    fn set_null(&mut self, name: NameId) -> Result<(), String> {
        let n = self.name(name);
        self.log.push(format!("null {n}"));
        self.scopes[self.scope].insert(n, V::None);
        Ok(())
    }

    fn define(&mut self, names: &[NameId], value: V, visibility: Visibility) -> Result<(), String> {
        let label: Vec<_> = names.iter().map(|&n| self.name(n)).collect();
        let global = if visibility == Visibility::Global { " global" } else { "" };
        self.log.push(format!("define{global} {}", label.join(",")));
        if visibility == Visibility::Global {
            for n in &label {
                self.rcontext.insert(n.clone(), value.clone());
            }
        }
        self.assign(names, value, Visibility::Local)
    }

    fn restore(&mut self, name: NameId, backup: Option<V>) -> Result<(), String> {
        let n = self.name(name);
        self.log.push(format!("restore {n}"));
        match backup {
            Some(v) => self.scopes[self.scope].insert(n, v),
            None => self.scopes[self.scope].remove(&n),
        };
        Ok(())
    }

    fn text_value(&mut self, text: Option<&str>) -> V {
        text.map_or(V::None, |t| V::Str(t.to_owned()))
    }
}

impl Loops for World {
    fn repeat(&mut self, names: &[NameId], iterable: V) -> Result<(V, Countdown<V>), String> {
        self.log.push(format!("repeat {}", self.name(names[0])));
        let V::List(items) = iterable else { return Err("not iterable".into()) };
        let len = items.len();
        self.iterators += 1;
        let len = i64::try_from(len).unwrap();
        // Loops over a name starting with `v` get a runtime-valued countdown.
        let index =
            if self.name(names[0]).starts_with('v') { Countdown::Value(V::Int(len)) } else { Countdown::Int(len) };
        Ok((V::Iter(items, self.iterators), index))
    }

    fn countdown(&mut self, index: &V) -> Result<(V, bool), String> {
        let V::Int(n) = index else { return Err("TypeError: unsupported operand".into()) };
        Ok((V::Int(n - 1), n - 1 > 0))
    }

    fn next(&mut self, iterator: &V) -> Result<Option<V>, String> {
        // The toy keeps the position in the scope under a hidden key, since values are cloned.
        let V::Iter(items, id) = iterator else { return Err("not an iterator".into()) };
        let key = format!("#it{id}");
        let pos = match self.scopes[self.scope].get(&key) {
            Some(V::Int(p)) => usize::try_from(*p).unwrap(),
            _ => 0,
        };
        let item = items.get(pos).cloned();
        if item.is_some() {
            self.scopes[self.scope].insert(key, V::Int(i64::try_from(pos + 1).unwrap()));
        } else {
            self.scopes[self.scope].remove(&key);
        }
        Ok(item)
    }

    fn assign(&mut self, names: &[NameId], item: V, visibility: Visibility) -> Result<(), String> {
        let items = if names.len() == 1 {
            vec![item]
        } else {
            let V::List(items) = item else { return Err("cannot unpack".into()) };
            items
        };
        for (&n, v) in names.iter().zip(items) {
            let n = self.name(n);
            if visibility == Visibility::Global {
                self.rcontext.insert(n.clone(), v.clone());
            }
            self.scopes[self.scope].insert(n, v);
        }
        Ok(())
    }

    fn missing_backup(&mut self, name: NameId) -> String {
        format!("NameError: __backup_{}", self.name(name))
    }
}

impl Errors for World {
    fn is_catchable(&mut self, error: &String) -> bool {
        !error.starts_with("KeyboardInterrupt")
    }

    fn catch(&mut self, error: &String, name: NameId, at: crate::ErrorSite) -> Result<(), String> {
        let n = self.name(name);
        self.log.push(format!("catch {error} at e{}", at.last.map_or(u32::MAX, |e| e.0)));
        if self.scopes[self.scope].get("handler_fails") == Some(&V::Bool(true)) {
            return Err(format!("HandlerError: {error}"));
        }
        self.scopes[self.scope].insert(n, V::Str(error.clone()));
        Ok(())
    }

    fn record_error(&mut self, error: &String, at: crate::ErrorSite) {
        let token = at.last.map_or(0, |e| e.0 + 1);
        self.log.push(format!("record {error} in program {} at token {token}", self.code));
    }

    fn recursion_error(&mut self) -> String {
        "RecursionError".into()
    }
}

impl Attrs for World {
    fn yields_name(&mut self, container: &V, name: &str) -> Result<bool, String> {
        let V::Dict(items) = container else { return Err("TypeError: not iterable".into()) };
        Ok(items.iter().any(|(k, _)| k == name))
    }

    fn attr_dict(&mut self, dict: &V, exclude: &[&str], bool_names: &[&str], out: &mut Output) -> Result<(), String> {
        let V::Dict(items) = dict else { return Err("AttributeError: items".into()) };
        for (name, value) in items {
            let mut value = value.clone();
            if bool_names.contains(&name.as_str()) {
                if !self.truthy(&value)? {
                    continue;
                }
                value = V::Str(name.clone());
            }
            if exclude.contains(&name.as_str()) || value == V::None {
                continue;
            }
            out.push_str(&format!(" {name}=\""));
            self.emit(value, Escape::Attr(crate::Quote::Double), Stage::Lone, out)?;
            out.push_str("\"");
        }
        Ok(())
    }

    fn case_compare(&mut self, value: &V, switch: Option<&V>) -> Result<V, String> {
        Ok(V::Bool(match switch {
            Some(s) => value == s,
            None => *value == V::Default,
        }))
    }
}

impl Macros for World {
    fn macro_call(&mut self, value: V, _: &mut Output) -> Result<MacroStep<V>, String> {
        let V::Macro(program, entry) = value else { return Err(format!("AttributeError: include of {value:?}")) };
        let scope = self.copy_scope()?;
        Ok(MacroStep::Frame {
            call: Call { program: Arc::clone(&self.programs[program].0), code: V::Code(program), scope },
            entry,
            cost: 1,
        })
    }

    fn copy_scope(&mut self) -> Result<V, String> {
        self.scopes.push(self.scopes[self.scope].clone());
        Ok(V::Scope(self.scopes.len() - 1))
    }

    fn after_macro(&mut self) -> Result<(), String> {
        let globals: Vec<_> = self.rcontext.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
        self.scopes[self.scope].extend(globals);
        Ok(())
    }

    fn fill_slot(&mut self, name: &str, fill: Fill<V>, extend: bool) -> Result<(), String> {
        self.fills.push(fill);
        let fill = V::Fill(self.fills.len() - 1);
        let key = format!("__slot_{name}");
        match self.scopes[self.scope].get_mut(&key) {
            Some(V::List(stack)) if extend => stack.insert(0, fill),
            _ => {
                self.scopes[self.scope].insert(key, V::List(vec![fill]));
            }
        }
        Ok(())
    }

    fn pop_slot(&mut self, name: &str) -> Option<V> {
        match self.scopes[self.scope].get_mut(&format!("__slot_{name}")) {
            Some(V::List(stack)) => stack.pop(),
            _ => None,
        }
    }

    fn slot_call(&mut self, slot: &V, _: &mut Output) -> Result<Option<Fill<V>>, String> {
        let V::Fill(i) = slot else { return Err("not a fill".into()) };
        let f = &self.fills[*i];
        Ok(Some(Fill {
            program: Arc::clone(&f.program),
            code: f.code.clone(),
            entry: f.entry,
            i18n: I18n {
                domain: f.i18n.domain.clone(),
                context: f.i18n.context.clone(),
                target_language: f.i18n.target_language.clone(),
            },
            slots: f.slots.clone(),
            stream: f.stream,
        }))
    }
}

impl Translate for World {
    fn i18n_changed(&mut self, i18n: &I18n<V>) -> Result<(), String> {
        self.i18n = i18n_label(i18n);
        Ok(())
    }

    fn translate(&mut self, call: Translation<'_, V>) -> Result<V, String> {
        let text = |v: &V| match v {
            V::Str(s) => s.clone(),
            other => format!("{other:?}"),
        };
        Ok(V::Str(match call {
            Translation::Block { msgid, mapping, default } => {
                let mapping: Vec<String> = mapping
                    .unwrap_or_default()
                    .into_iter()
                    .map(|(k, v)| match v {
                        NameValue::Text(t) => format!("{k}={t}"),
                        NameValue::Pieces(p) => {
                            format!("{k}={:?}", p.iter().map(ToString::to_string).collect::<Vec<_>>())
                        }
                    })
                    .collect();
                format!("tr({msgid}|{}|{default}|{})", mapping.join(","), self.i18n)
            }
            Translation::Content(v) => format!("tr({}|{})", text(&v), self.i18n),
            Translation::Attr { msgid, value } => format!("tr({msgid:?}|{}|{})", text(&value), self.i18n),
            Translation::Interp { msgid, mapping } => {
                let mapping: Vec<String> =
                    mapping.unwrap_or_default().iter().map(|(k, v)| format!("{k}={}", text(v))).collect();
                format!("tr({msgid}|{}|{})", mapping.join(","), self.i18n)
            }
        }))
    }

    fn quote_value(&mut self, value: V, _: Escape, default: Option<&str>) -> Result<V, String> {
        Ok(match value {
            V::Default => default.map_or(V::None, |d| V::Str(d.to_owned())),
            other => other,
        })
    }

    fn convert_value(&mut self, value: V) -> Result<V, String> {
        Ok(match value {
            V::Default => V::None,
            other => other,
        })
    }
}

fn world(templates: &[&str], vars: &[(&str, V)]) -> World {
    let programs = templates
        .iter()
        .map(|t| {
            let mut toy = Toy::default();
            let p = compile(t, &mut toy, &options()).unwrap_or_else(|e| panic!("{t}: {e:?}"));
            (Arc::new(p), toy.exprs)
        })
        .collect();
    let scope = vars.iter().map(|(k, v)| ((*k).to_owned(), v.clone())).collect();
    World {
        programs,
        scopes: vec![scope],
        code: 0,
        scope: 0,
        log: Vec::new(),
        iterators: 0,
        rcontext: HashMap::new(),
        fills: Vec::new(),
        i18n: String::new(),
        serials: Vec::new(),
    }
}

fn options() -> Options {
    Options { boolean_attributes: vec!["checked".into(), "selected".into()], ..Options::default() }
}

fn render_world(w: &mut World) -> String {
    let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0), None);
    loop {
        match run(&mut st, w).unwrap() {
            Done::Finished => break,
            Done::Suspended(v) => st.resume_with::<String>(v).unwrap(),
        }
    }
    assert_eq!(st.unwind_depth(), 0);
    assert_eq!(st.frame_depth(), 0);
    String::from_utf8(st.out.as_bytes().to_vec()).unwrap()
}

fn render(template: &str, vars: &[(&str, V)]) -> String {
    render_world(&mut world(&[template], vars))
}

#[test]
fn static_text_is_coalesced() {
    let mut toy = Toy::default();
    let p = compile("<div class=\"a\">\n  <p>hi</p><!-- c --></div>", &mut toy, &options()).unwrap();
    insta::assert_snapshot!(p.dump(), @r#"
      0 text "<div class=\"a\">\n  <p>hi</p><!-- c --></div>"
      1 end
    "#);
}

#[test]
fn listing_bytecode() {
    let mut toy = Toy::default();
    let src = "<ul>\n  <li tal:repeat=\"v vs\" class=\"x ${v}\"><a href=\"${v}\" tal:content=\"v\">d</a></li>\n</ul>";
    let p = compile(src, &mut toy, &options()).unwrap();
    insta::assert_snapshot!(p.dump(), @r#"
      0 text "<ul>\n  "
      1 loop ["v"] in e0 ws="\n  " next=10
      2 text "<li class=\"x "
      3 emit e1 Attr(Double) Part
      4 text "\"><a"
      5 attr e2 Attr(Double) " href=\"" "\""
      6 text ">"
      7 content e3 Content end=9
      8 text "d"
      9 text "</a></li>"
     10 loop_next body=2
     11 text "\n</ul>"
     12 end
    "#);
}

#[test]
fn interpolation_and_attributes() {
    let vars = [("x", V::Str("a&<b>".into())), ("n", V::None)];
    assert_eq!(
        render("<p a=\"${x}\" b=\"${n}\" c=\"[${n}]\">${x}${n}</p>", &vars),
        "<p a=\"a&amp;&lt;b&gt;\" c=\"[]\">a&amp;&lt;b&gt;</p>"
    );
    assert_eq!(render("<p a=\"${default}\">${'d'}</p>", &vars), "<p>d</p>");
}

#[test]
fn content_and_default() {
    assert_eq!(render("<p tal:content=\"default\">keep <b>me</b></p>", &[]), "<p>keep <b>me</b></p>");
    assert_eq!(render("<p tal:content=\"none\">gone</p>", &[]), "<p></p>");
    assert_eq!(render("<p tal:content=\"'x'\"/>", &[]), "<p>x</p>");
    assert_eq!(render("<p tal:content=\"structure '<b>'\">d</p>", &[]), "<p><b></p>");
}

#[test]
fn condition_wraps_repeat() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)])), ("t", V::Int(1)), ("f", V::Int(0))];
    assert_eq!(render("<i tal:condition=\"f\">no</i><i tal:condition=\"t\">yes</i>", &vars), "<i>yes</i>");
    assert_eq!(render("<i tal:condition=\"xs\" tal:repeat=\"x xs\">${x}</i>", &vars), "<i>1</i>\n<i>2</i>");
}

#[test]
fn repeat_whitespace_and_restore_order() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2), V::Int(3)])), ("x", V::Str("outer".into()))];
    let mut w = world(&["<ul>\n    <li tal:repeat=\"x xs\">${x}</li>\n</ul>${x}"], &vars);
    assert_eq!(render_world(&mut w), "<ul>\n    <li>1</li>\n    <li>2</li>\n    <li>3</li>\n</ul>outer");
    assert_eq!(w.log, ["backup x", "repeat x", "null x", "restore x"]);

    assert_eq!(render("<p tal:repeat=\"x []\">${x}</p>|", &[]), "|");
    assert_eq!(render("<div>\n  <tal:b repeat=\"x xs\">${x}</tal:b>\n</div>", &vars), "<div>\n  123\n</div>");
    let pairs = V::List(vec![V::List(vec![V::Int(1), V::Int(2)]), V::List(vec![V::Int(3), V::Int(4)])]);
    assert_eq!(render("<p tal:repeat=\"(a, b) ps\">${a}${b}</p>", &[("ps", pairs)]), "<p>12</p>\n<p>34</p>");
}

#[test]
fn nested_repeat() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)])), ("ys", V::List(vec![V::Int(7), V::Int(8)]))];
    assert_eq!(
        render("<div>\n  <p tal:repeat=\"x xs\">\n    <b tal:repeat=\"y ys\">${x}${y}</b>\n  </p>\n</div>", &vars),
        "<div>\n  <p>\n    <b>17</b>\n    <b>18</b>\n  </p>\n  <p>\n    <b>27</b>\n    <b>28</b>\n  </p>\n</div>"
    );
}

#[test]
fn frames_write_into_the_caller_buffer() {
    let mut w = world(&["<p a=\"${call:1}\">${call:1}|${x}</p>", "<i>${x}</i>"], &[("x", V::Str("main".into()))]);
    assert_eq!(render_world(&mut w), "<p a=\"<i>from 1</i>\"><i>from 1</i>|main</p>");
}

#[test]
fn infinite_recursion_is_an_error() {
    let mut w = world(&["${call:0}"], &[]);
    let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0), None);
    assert!(matches!(run(&mut st, &mut w), Err(VmError::Runtime(e)) if e == "RecursionError"));
    // Every frame records the error on its way out (a partial's render nests four
    // interpreter frames in Chameleon).
    assert_eq!(
        w.log.iter().filter(|l| l.starts_with("record RecursionError")).count(),
        crate::DEFAULT_FRAME_BUDGET / 4 + 1
    );
}

#[test]
fn suspend_and_resume() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)]))];
    assert_eq!(
        render(
            "<p a=\"${wait:'q'}\" tal:condition=\"wait:1\">${wait:'z'}<b tal:repeat=\"x wait:xs\">${x}</b>\
             <i tal:content=\"wait:'c'\">d</i></p>",
            &vars
        ),
        "<p a=\"q\">z<b>1</b>\n           <b>2</b><i>c</i></p>"
    );
}

#[test]
fn non_strict_mode_defers_syntax_errors_to_the_site() {
    let lax = Options { strict: false, ..options() };
    let run = |src: &str, vars: &[(&str, V)]| {
        let mut toy = Toy::default();
        let p = compile(src, &mut toy, &lax).unwrap();
        let mut w = world(&["x"], vars);
        w.programs[0] = (Arc::new(p), toy.exprs);
        let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0), None);
        run(&mut st, &mut w).map(|_| String::from_utf8(st.out.as_bytes().to_vec()).unwrap())
    };
    // Not reached: renders.
    assert_eq!(run("<p tal:condition=\"false\" tal:content=\"'\">x</p>ok", &[]).unwrap(), "ok");
    // Reached: the deferred error, about the whole site's token, and nothing of the
    // interpolation (`a`, `missing`) is evaluated first.
    let err = run("<p>${a} ${ }</p>", &[]).unwrap_err();
    assert!(matches!(&err, VmError::Runtime(m) if m.starts_with("ExpressionError: invalid")), "{err:?}");
    let err = run("<p tal:content=\"'\">x</p>", &[]).unwrap_err();
    assert!(matches!(&err, VmError::Runtime(m) if m.contains("@ \"'\"")), "{err:?}");
}

#[test]
fn compile_errors() {
    let err = |src: &str| compile(src, &mut Toy::default(), &options()).unwrap_err().to_string();
    assert_eq!(err("<p>${ }</p>"), "invalid: \"\"");
    assert_eq!(err("<p tal:foo=\"x\"/>"), "Bad attribute for namespace 'http://xml.zope.org/namespaces/tal'");
    assert_eq!(err("<p tal:repeat=\"econtext xs\"/>"), "Name disallowed by compiler.");
    assert_eq!(err("<p tal:define=\"__x 1\"/>"), "Name disallowed by compiler (double underscore).");
    assert_eq!(err("<p>a</b>"), "Unexpected end tag.");
    assert_eq!(err("<?python x = 1 ?>"), "expression compiler error");
    assert_eq!(err("<a foo:b=\"1\"/>"), "Undefined namespace prefix: foo.");
    assert_eq!(err("<p tal:case=\"1\"/>"), "Must define switch on a parent element.");
    assert!(
        matches!(compile("${boom}", &mut Toy::default(), &options()), Err(crate::CompileError::Expr(e)) if e == "boom")
    );
}

#[test]
fn compile_with_given_tokens() {
    let src = "<p>${x}</p><b>t</b>";
    let spans = |cuts: &[usize]| cuts.windows(2).map(|w| crate::Span::new(w[0], w[1])).collect::<Vec<_>>();
    // The tokenizer's tokens and a coarser split of the same text both compile.
    assert!(
        crate::compile_tokens(src, Some(spans(&[0, 3, 7, 11, 14, 15, 19])), &mut Toy::default(), &options()).is_ok()
    );
    // Tokens out of order, overlapping, past the end or inside a character are refused.
    for bad in [vec![crate::Span::new(3, 7), crate::Span::new(0, 3)], vec![crate::Span::new(0, 30)]] {
        let err = crate::compile_tokens(src, Some(bad), &mut Toy::default(), &options()).unwrap_err();
        assert_eq!(err.to_string(), "tokens must be ordered, non-overlapping slices of the source");
    }
    let err = crate::compile_tokens("é", Some(vec![crate::Span::new(0, 1)]), &mut Toy::default(), &options());
    assert!(err.is_err());
}

#[test]
fn define_backs_up_before_evaluating_and_restores_in_reverse() {
    let vars = [("x", V::Str("outer".into()))];
    let mut w = world(&["<p tal:define=\"x 1; y x; global g 'G'\">${x}${y}${g}</p>${x}${g}"], &vars);
    assert_eq!(render_world(&mut w), "<p>11G</p>outerG");
    assert_eq!(w.log, ["backup x", "define x", "backup y", "define y", "define global g", "restore y", "restore x"]);
    let pairs = [("p", V::List(vec![V::Int(1), V::Int(2)]))];
    assert_eq!(render("<i tal:define=\"(a, b) p\">${a}${b}</i>", &pairs), "<i>12</i>");
}

#[test]
fn attrs_alias_is_the_elements_static_attributes() {
    assert_eq!(render("<p a=\"1\" tal:define=\"s attrs\" tal:content=\"s\"/>", &[]), "<p a=\"1\">a=1</p>");
    assert_eq!(render("<p b=\"2\"><i tal:content=\"attrs\"/>${attrs}</p>", &[]), "<p b=\"2\"><i></i>b=2</p>");
    let mut toy = Toy::default();
    compile("${attrs}", &mut toy, &options()).unwrap();
    assert_eq!(toy.exprs[0].attrs, None);
}

#[test]
fn omit_tag() {
    let vars = [("t", V::Int(1)), ("f", V::Int(0))];
    assert_eq!(render("<p tal:omit-tag=\"\">a</p><p tal:omit-tag=\"t\" x=\"${t}\">b</p>", &vars), "ab");
    assert_eq!(render("<p tal:omit-tag=\"f\">c</p><br tal:omit-tag=\"f\"/>", &vars), "<p>c</p><br/>");
}

#[test]
fn replace_with_default() {
    assert_eq!(render("<p tal:replace=\"'x'\">a</p>|<p tal:replace=\"default\">b</p>", &[]), "x|<p>b</p>");
}

#[test]
fn dynamic_attributes() {
    let vars = [("t", V::Int(1)), ("f", V::Int(0)), ("n", V::None)];
    assert_eq!(
        render("<p a=\"1\" tal:attributes=\"a 'x'; b n; c default; d 'y'\">t</p>", &vars),
        "<p a=\"x\" d=\"y\">t</p>"
    );
    assert_eq!(render("<p a=\"1\" tal:attributes=\"a default\">t</p>", &vars), "<p a=\"1\">t</p>");
    // Boolean attributes: true takes the name, false drops, default keeps the static value.
    assert_eq!(
        render(
            "<i checked=\"yes\" tal:attributes=\"checked t; selected f\"/><i checked=\"c\" tal:attributes=\"checked default\"/>",
            &vars
        ),
        "<i checked=\"checked\"/><i checked=\"c\"/>"
    );
    assert_eq!(
        render("<i checked=\"${t}\" selected=\"${f}\"/><i checked=\"${f}${t}\"/>", &vars),
        "<i checked=\"checked\"/><i checked=\"checked\"/>"
    );
    assert_eq!(render("<i checked=\"${f}${n}\" selected=\"a${n}\"/>", &vars), "<i selected=\"selected\"/>");
}

#[test]
fn dictionary_attributes_and_filters() {
    let vars =
        [("d", V::Dict(vec![("a".into(), V::Str("A".into())), ("checked".into(), V::Int(0)), ("z".into(), V::None)]))];
    // `a` is written by the dictionary, so the static `a` before it is filtered out.
    // The dictionary goes after the written attributes (Chameleon appends it).
    assert_eq!(render("<p a=\"1\" tal:attributes=\"d\" b=\"2\">t</p>", &vars), "<p b=\"2\" a=\"A\">t</p>");
    assert_eq!(render("<p a=\"${1}\" tal:attributes=\"d\">t</p>", &vars), "<p a=\"A\">t</p>");
}

#[test]
fn switch_and_case() {
    let src =
        "<div tal:switch=\"x\"><b tal:case=\"1\">one</b><b tal:case=\"2\">two</b><b tal:case=\"default\">d</b></div>";
    assert_eq!(render(src, &[("x", V::Int(2))]), "<div><b>two</b></div>");
    assert_eq!(render(src, &[("x", V::Int(9))]), "<div><b>d</b></div>");
    assert_eq!(render(src, &[("x", V::Int(1))]), "<div><b>one</b></div>");
    // An own-element switch is not cached before the case: Chameleon's assertion fails.
    assert_eq!(
        compile("<p tal:switch=\"1\" tal:case=\"1\"/>", &mut Toy::default(), &options()).unwrap_err().kind(),
        Some(crate::ErrorKind::Assertion)
    );
}

#[test]
fn runtime_valued_countdown() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2), V::Int(3)]))];
    assert_eq!(render("<i tal:repeat=\"v xs\">${v}</i>", &vars), "<i>1</i>\n<i>2</i>\n<i>3</i>");
}

#[test]
fn loops_in_callee_frames_keep_their_own_unwind_entries() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)]))];
    let mut w = world(&["<b tal:repeat=\"x xs\">${x}${call:1}${x}</b>", "<i tal:repeat=\"y [7, 8]\">${y}</i>"], &vars);
    assert_eq!(render_world(&mut w), "<b>1<i>7</i>\n<i>8</i>1</b>\n<b>2<i>7</i>\n<i>8</i>2</b>");
}

/// Runs until the first suspension and returns the state.
fn run_to_suspension(w: &mut World) -> (VmState<V>, V) {
    let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0), None);
    match run(&mut st, w).unwrap() {
        Done::Suspended(v) => (st, v),
        Done::Finished => panic!("did not suspend"),
    }
}

#[test]
fn suspend_inside_a_callee_frame() {
    // The callee suspends in an attribute and in text; the caller's attribute closing quote
    // (on_return) is written only when the callee returns.
    let mut w = world(&["<p a=\"${call:1}\">${call:1}</p>", "<i t=\"${wait:'q'}\">${wait:'z'}</i>"], &[]);
    let (mut st, _) = run_to_suspension(&mut w);
    assert_eq!(st.frame_depth(), 2);
    let mut values = 0;
    st.for_each_value(|_| values += 1);
    assert_eq!(values, 4); // two frames, code and scope each
    st.resume_with::<String>(V::Str("q".into())).unwrap();
    let mut out = String::new();
    loop {
        match run(&mut st, &mut w).unwrap() {
            Done::Finished => break,
            Done::Suspended(v) => st.resume_with::<String>(v).unwrap(),
        }
    }
    out.push_str(std::str::from_utf8(st.out.as_bytes()).unwrap());
    assert_eq!(out, "<p a=\"<i t=\"q\">z</i>\"><i t=\"q\">z</i></p>");
}

#[test]
fn suspend_inside_a_loop_body_with_a_frame_call() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)]))];
    let mut w = world(&["<b tal:repeat=\"x xs\">${wait:x}${call:1}</b>", "<i>${wait:x}</i>"], &vars);
    let (st, _) = run_to_suspension(&mut w);
    assert_eq!(st.unwind_depth(), 1);
    let mut values = 0;
    st.for_each_value(|_| values += 1);
    assert_eq!(values, 3); // frame code and scope, the loop iterator (no backup: `x` was unbound)
    assert_eq!(render_world(&mut w), "<b>1<i>from 1</i></b>\n<b>2<i>from 1</i></b>");
}

#[test]
fn resume_and_run_are_checked() {
    let mut w = world(&["${wait:1}"], &[]);
    let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0), None);
    assert!(matches!(st.resume_with::<String>(V::None), Err(VmError::Internal(_))));
    assert!(matches!(run(&mut st, &mut w), Ok(Done::Suspended(_))));
    assert!(matches!(run(&mut st, &mut w), Err(VmError::Internal(_))));
    st.resume_with::<String>(V::Int(1)).unwrap();
    assert!(matches!(run(&mut st, &mut w), Ok(Done::Finished)));
}

#[test]
fn capacity_hint_is_smoothed_and_capped() {
    let p = compile("x", &mut Toy::default(), &options()).unwrap();
    p.record_size(1000);
    assert_eq!(p.capacity_hint(), 1000);
    p.record_size(usize::MAX / 2);
    assert_eq!(p.capacity_hint(), crate::MAX_CAPACITY_HINT);
    p.record_size(0);
    assert_eq!(p.capacity_hint(), crate::MAX_CAPACITY_HINT / 2);
}

#[test]
fn declarations_processing_instructions_and_cdata_pass_through() {
    // Verified against Chameleon: a processing instruction is visited as text (`$$` unescaped),
    // CDATA and declarations are output as is, a missing `>` adds nothing.
    assert_eq!(
        render("<!DOCTYPE html>\n<?php $$x ?><![CDATA[ $$ ]]><p>a</p\n<b", &[]),
        "<!DOCTYPE html>\n<?php $x ?><![CDATA[ $$ ]]><p>a</p<b"
    );
}

/// The program tree of every upstream fixture, as a snapshot (a regression guard; exactness
/// is checked against Chameleon by the pytest differential tests). Options follow the
/// upstream harness (`test_templates.execute`): implicit i18n and data attributes by file
/// name, the HTML boolean attributes and newline normalization unless the fixture is XML.
#[test]
fn fixture_trees() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/upstream/chameleon_tests/inputs");
    let mut paths: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "pt" || e == "txt"))
        .collect();
    paths.sort();
    assert!(paths.len() > 100, "fixtures not found in {}", dir.display());
    let html_booleans = [
        "compact", "nowrap", "ismap", "declare", "noshade", "checked", "disabled", "readonly", "multiple", "selected",
        "noresize", "defer",
    ];
    let mut built = 0;
    for path in &paths {
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        // Not UTF-8 (074 is windows-1251): decoding needs Python's codecs; pytest covers it.
        let Ok(text) = String::from_utf8(std::fs::read(path).unwrap()) else { continue };
        let text = text.strip_prefix('\u{feff}').unwrap_or(&text).to_owned();
        let xml = text.starts_with("<?xml");
        let text = if xml { text } else { text.replace("\r\n", "\n").replace('\r', "\n") };
        let implicit = name.contains("implicit-i18n");
        let text_mode = path.extension().is_some_and(|e| e.eq_ignore_ascii_case("txt"));
        let options = Options {
            mode: if text_mode { crate::Mode::Text } else { crate::Mode::Xml },
            escape: !text_mode,
            boolean_attributes: if xml { Vec::new() } else { html_booleans.map(String::from).to_vec() },
            implicit_i18n_translate: implicit,
            implicit_i18n_attributes: if implicit { vec!["alt".into(), "title".into()] } else { Vec::new() },
            enable_data_attributes: name.contains("data-attributes"),
            strict: false,
            ..Options::default()
        };
        let dump = match crate::compile::build_tree(&text, &options) {
            Ok(tree) => {
                built += 1;
                crate::nodes::dump(&tree, &text)
            }
            Err(e) => match e {
                crate::CompileError::Template { kind, message, token } => {
                    let (t, pos) = token.resolve(&text);
                    let at = pos.map(|p| p.offset(&crate::Source::new(&text)));
                    format!("error {kind:?}: {message} token={t:?} at={at:?}\n")
                }
                other => format!("error {other}\n"),
            },
        };
        insta::with_settings!({ snapshot_path => "snapshots/fixtures", prepend_module_to_snapshot => false }, {
            insta::assert_snapshot!(name, dump);
        });
    }
    assert!(built > 100, "only {built} fixtures built");
}

#[test]
fn instructions_stay_small() {
    // The VM's hot loop reads instructions back to back; keep them at Phase 2's size.
    assert_eq!(std::mem::size_of::<crate::Op>(), 24);
}

/// Renders, returning the output or the error that escaped.
fn try_render(w: &mut World) -> Result<String, String> {
    let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0), None);
    loop {
        match run(&mut st, w) {
            Ok(Done::Finished) => break,
            Ok(Done::Suspended(v)) => st.resume_with::<String>(v).unwrap(),
            Err(VmError::Runtime(e)) => return Err(e),
            Err(e) => panic!("{e:?}"),
        }
    }
    assert_eq!(st.unwind_depth(), 0);
    Ok(String::from_utf8(st.out.as_bytes().to_vec()).unwrap())
}

#[test]
fn on_error_drops_defines_and_loops_without_restoring() {
    // Chameleon's generated code has no try/finally: a failed body's defines and loop names
    // keep their values (plan.md, Phase 6a). Written first.
    let src = "<div tal:on-error=\"x\"><p tal:define=\"x 2\"><b tal:repeat=\"i [7]\">${i}${undefined}</b></p></div>|${x}|${i}";
    let mut w = world(&[src], &[]);
    assert_eq!(try_render(&mut w).unwrap(), "<div>2</div>|2|7");
    assert!(!w.log.iter().any(|l| l.starts_with("restore")), "{:?}", w.log);
    // The variable `error` is bound and stays bound.
    let src = "<p tal:on-error=\"error\">${nope}</p>|${error}";
    assert_eq!(render(src, &[]), "<p>NameError: nope</p>|NameError: nope");
}

#[test]
fn on_error_truncates_to_the_frames_last_fallback_mark() {
    // One `__fallback` register per render function: once an inner handler has started, an
    // outer one truncates only to the inner one's mark.
    let src = "<p tal:on-error=\"'A'\">a<b tal:on-error=\"'x'\">B</b>C${nope}</p>";
    assert_eq!(render(src, &[]), "<p>a<p>A</p>");
    // A failing fallback is caught by the outer handler, after the inner mark.
    let src = "<p tal:on-error=\"'outer'\"><b tal:on-error=\"nope\">${nope}</b></p>";
    assert_eq!(render(src, &[]), "<p><p>outer</p>");
    // Siblings and the ordinary case.
    let src = "<i tal:on-error=\"'1'\">${a}</i><i tal:on-error=\"'2'\">ok</i><i tal:on-error=\"'3'\">${c}</i>";
    assert_eq!(render(src, &[]), "<i>1</i><i>ok</i><i>3</i>");
    // Omitted tags, replace, and an on-error around a TAL element.
    assert_eq!(render("<p tal:on-error=\"'a'\" tal:omit-tag=\"\">x${nope}</p>", &[]), "a");
    assert_eq!(render("<tal:p on-error=\"'a'\">x${nope}</tal:p>", &[]), "a");
    assert_eq!(render("<p tal:on-error=\"'a'\" tal:replace=\"nope\">x</p>", &[]), "<p>a</p>");
}

#[test]
fn on_error_entries_and_handlers() {
    // Cached values, switches, omit tests and attribute marks above the handler are dropped.
    let src = "<div tal:on-error=\"'E'\"><p tal:switch=\"1\"><b tal:case=\"1\" tal:omit-tag=\"true\" \
               tal:attributes=\"dict(a=1)\" checked=\"${t}\">${nope}</b></p></div>|ok";
    assert_eq!(render(src, &[("t", V::Int(1))]), "<div>E</div>|ok");
    let src = "<div tal:on-error=\"'E'\"><p tal:switch=\"1\"><b tal:case=\"1\" tal:omit-tag=\"false\" \
               tal:attributes=\"dict(a=1)\" checked=\"a${nope}\">x</b></p></div>|ok";
    assert_eq!(render(src, &[]), "<div>E</div>|ok");
    // An error the handlers do not catch passes through every frame, recording it in each.
    let mut w = world(&["<p tal:on-error=\"'E'\">${call:1}</p>", "<i>${interrupt}</i>"], &[]);
    assert_eq!(try_render(&mut w).unwrap_err(), "KeyboardInterrupt");
    assert_eq!(
        w.log,
        ["record KeyboardInterrupt in program 1 at token 1", "record KeyboardInterrupt in program 0 at token 2"]
    );
    // A callee's error is caught by the caller's handler, after the callee recorded it.
    let mut w = world(&["<p tal:on-error=\"'E'\">a${call:1}b</p>|${error}", "<i>${nope}</i>"], &[]);
    assert_eq!(try_render(&mut w).unwrap(), "<p>E</p>|NameError: nope");
    assert_eq!(
        w.log,
        ["record NameError: nope in program 1 at token 1", "catch NameError: nope at e1"],
        "the callee recorded its own token; the caller's token is the call"
    );
    // A handler that raises: the error leaves the body's frame.
    let mut w = world(&["<p tal:on-error=\"'E'\">${nope}</p>"], &[("handler_fails", V::Bool(true))]);
    assert_eq!(try_render(&mut w).unwrap_err(), "HandlerError: NameError: nope");
}

#[test]
fn on_error_restarts_after_suspension() {
    let src = "<p tal:on-error=\"wait:'E'\">${wait:'a'}${nope}</p>|${wait:'z'}";
    assert_eq!(render(src, &[]), "<p>E</p>|z");
}

#[test]
fn global_repeat_assigns_rcontext_and_ends_with_a_name_error() {
    let vars = [("xs", V::List(vec![V::Int(1), V::Int(2)]))];
    let mut w = world(&["<p tal:repeat=\"global x xs\">${x}</p>"], &vars);
    assert_eq!(try_render(&mut w).unwrap_err(), "NameError: __backup_x");
    assert_eq!(w.rcontext.get("x"), Some(&V::Int(2)));
    assert!(!w.log.iter().any(|l| l.starts_with("backup")));
    // Inside on-error, the loop's names leak.
    let mut w = world(&["<div tal:on-error=\"'E'\"><p tal:repeat=\"global x xs\">${x}</p></div>${x}"], &vars);
    assert_eq!(try_render(&mut w).unwrap(), "<div>E</div>2");
}

#[test]
fn boolean_attributes_test_the_converted_value() {
    let vars = [("e", V::Str(String::new())), ("t", V::Str("yes".into()))];
    // The toy's `Stage::Bool`: an empty string is false but written.
    assert_eq!(render("<input checked=\"${e}\" selected=\"${t}\">", &vars), "<input selected=\"selected\">");
}

#[test]
fn throw_unwinds_from_the_suspended_instruction() {
    // A failed await inside on-error: the fallback is rendered and the render goes on.
    let mut w = world(&["<p tal:on-error=\"'E'\">a${wait:1}b</p>|${wait:'z'}"], &[]);
    let (mut st, _) = run_to_suspension(&mut w);
    let done = crate::throw(&mut st, &mut w, "ValueError: awaited".to_owned()).unwrap();
    let Done::Suspended(v) = done else { panic!("the second wait suspends") };
    st.resume_with::<String>(v).unwrap();
    assert!(matches!(run(&mut st, &mut w), Ok(Done::Finished)));
    assert_eq!(std::str::from_utf8(st.out.as_bytes()).unwrap(), "<p>E</p>|z");
    assert!(w.log.iter().any(|l| l.starts_with("catch ValueError: awaited")));

    // A non-catchable error (cancellation, BaseException-style) passes every handler.
    let mut w = world(&["<p tal:on-error=\"'E'\">a${wait:1}b</p>"], &[]);
    let (mut st, _) = run_to_suspension(&mut w);
    let result = crate::throw(&mut st, &mut w, "KeyboardInterrupt".to_owned());
    assert!(matches!(result, Err(VmError::Runtime(e)) if e == "KeyboardInterrupt"));
    assert_eq!(st.frame_depth(), 0);

    // Only a suspended render can be thrown into.
    let mut w = world(&["x"], &[]);
    let mut st = VmState::new(Arc::clone(&w.programs[0].0), V::Code(0), V::Scope(0), None);
    assert!(matches!(crate::throw(&mut st, &mut w, "E".to_owned()), Err(VmError::Internal(_))));
}

#[test]
fn macros_run_as_frames_with_a_copy_of_the_scope() {
    // The define-macro element renders in place; `use-macro` runs it again with `macroname`
    // bound, then `econtext.update(rcontext)` brings the macro's globals back.
    let src = "<div metal:define-macro=\"m\" tal:define=\"global g 'G'\">[${macroname}]</div>\
               <p tal:define=\"g 'local'\"><b metal:use-macro=\"macro:0:m\"/>${g}</p>";
    let mut w = world(&[src], &[("macroname", V::Str("outer".into()))]);
    assert_eq!(try_render(&mut w).unwrap(), "<div>[outer]</div><p><div>[macro:0:m]</div>G</p>");
    // Each frame gets its own serial (code blocks' namespaces are keyed by it).
    let mut serials = w.serials.clone();
    serials.dedup();
    assert!(serials.len() >= 3, "{serials:?}");
}

#[test]
fn slots_fill_extend_and_default() {
    let base =
        "<div metal:define-macro=\"base\">[<b metal:define-slot=\"a\">A</b>|<b metal:define-slot=\"b\">B</b>]</div>";
    let ext = "<div metal:define-macro=\"ext\" metal:extend-macro=\"macro:0:base\">\
               <i metal:fill-slot=\"a\">ext<u metal:define-slot=\"c\">C</u></i></div>";
    let page = "<p metal:use-macro=\"macro:1:ext\"><x metal:fill-slot=\"c\">page</x></p>|\
                <p metal:use-macro=\"macro:1:ext\"><x metal:fill-slot=\"a\">over</x></p>";
    let mut w = world(&[base, ext, page], &[]);
    let mut st = VmState::new(Arc::clone(&w.programs[2].0), V::Code(2), V::Scope(0), None);
    assert!(matches!(run(&mut st, &mut w), Ok(Done::Finished)));
    assert_eq!(
        std::str::from_utf8(st.out.as_bytes()).unwrap(),
        "<div>[<i>ext<x>page</x></i>|<b>B</b>]</div>|<div>[<x>over</x>|<b>B</b>]</div>"
    );
}

#[test]
fn macro_recursion_stops_at_the_frame_budget() {
    let src = "<tal:d condition=\"false\"><div metal:define-macro=\"r\"><p metal:use-macro=\"macro:0:r\"/></div></tal:d>\
               <x tal:on-error=\"'deep'\" metal:use-macro=\"macro:0:r\"/>";
    // (A `use-macro` element's tags are never written, so the fallback has none either.)
    assert_eq!(render(src, &[]), "deep");
    // Slot bodies end without recording (their functions have no error handler).
    let src = "<tal:d condition=\"false\"><div metal:define-macro=\"m\"><b metal:define-slot=\"s\"/></div></tal:d>\
               <p metal:use-macro=\"macro:0:m\"><i metal:fill-slot=\"s\">${nope}</i></p>";
    let mut w = world(&[src], &[]);
    assert_eq!(try_render(&mut w).unwrap_err(), "NameError: nope");
    assert_eq!(w.log.iter().filter(|l| l.starts_with("record")).count(), 2, "{:?}", w.log);
}

#[test]
fn translations_collect_their_streams_and_names() {
    let src = "<p i18n:domain=\"d\" i18n:translate=\"\">Hello  <b i18n:name=\"who\">${x}</b>\n !</p>\
               <p i18n:translate=\"msg\" i18n:context=\"c\">x</p><p i18n:translate=\"\"> </p>";
    assert_eq!(
        render(src, &[("x", V::Str("you".into()))]),
        "<p>tr(Hello ${who} !|who=<b>you</b>|Hello ${who} !|Some(\"d\")/None/None)</p>\
         <p>tr(msg||x|None/Some(\"c\")/None)</p><p></p>"
    );
    // An unfinished name caught inside the translation: its value is its stream.
    let src = "<p i18n:translate=\"\">a<b i18n:name=\"n\" tal:on-error=\"'E'\">b${nope}</b></p>";
    assert_eq!(render(src, &[]), "<p>tr(a<b>E</b>|n=[\"<b>b\"]|a<b>E</b>|None/None/None)</p>");
}

#[test]
fn a_slot_writes_to_the_stream_its_caller_had_open() {
    // Chameleon's fill function appends through its defining function's `__append`: a slot
    // inside a translation in the macro writes before the translation's result.
    let src = "<tal:d condition=\"false\"><div metal:define-macro=\"m\"><p i18n:translate=\"\">a \
               <b metal:define-slot=\"s\">d</b> c</p></div></tal:d>\
               <div metal:use-macro=\"macro:0:m\"><i metal:fill-slot=\"s\">F</i></div>";
    assert_eq!(render(src, &[]), "<div><p><i>F</i>tr(a c||a c|None/None/None)</p></div>");
}
