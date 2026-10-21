// Ported from Chameleon's src/chameleon/zpt/program.py (`MacroProgram`'s class attributes and
// `_pop_defaults`) and src/chameleon/template.py (`strict`), modified: a typed struct.

//! Template-level settings that change the program (Chameleon's `MacroProgram` keywords).

/// How the source is tokenized (`mode`).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum Mode {
    /// Markup (`iter_xml`): `PageTemplate`.
    #[default]
    Xml,
    /// Plain text (`iter_text`): `PageTextTemplate`.
    Text,
}

/// Settings that change how a template is built. `Default` gives `MacroProgram`'s defaults;
/// the template classes choose some differently (for example `PageTemplate` passes the HTML
/// boolean attributes unless the content type is `text/xml`, and `escape` follows the mode).
///
/// The `default_marker` option is a value, not a build setting: the runtime owns it.
#[derive(Debug, Clone, PartialEq, Eq)]
#[allow(clippy::struct_excessive_bools)] // Chameleon's independent on/off settings
pub struct Options {
    pub mode: Mode,
    /// Escape `&`, `<`, `>` in text and comment interpolation (`escape`; true in XML mode).
    pub escape: bool,
    /// Attribute names with boolean behaviour (`boolean_attributes`), compared exactly.
    pub boolean_attributes: Vec<String>,
    /// Translate text without explicit markup (`implicit_i18n_translate`).
    pub implicit_i18n_translate: bool,
    /// Attributes translated without explicit markup (`implicit_i18n_attributes`), compared
    /// with the lower-cased attribute name.
    pub implicit_i18n_attributes: Vec<String>,
    /// Collapse whitespace at line ends around tags and attributes (`trim_attribute_space`).
    pub trim_attribute_space: bool,
    /// `data-tal-*` style attributes (`enable_data_attributes`).
    pub enable_data_attributes: bool,
    /// `${...}` in comments (`enable_comment_interpolation`).
    pub enable_comment_interpolation: bool,
    /// Undeclared namespace prefixes are errors (`restricted_namespace`).
    pub restricted_namespace: bool,
    /// Expression syntax errors raise at compile time (`strict`); otherwise they raise when
    /// the expression is reached at render time.
    pub strict: bool,
}

impl Default for Options {
    fn default() -> Self {
        Self {
            mode: Mode::Xml,
            escape: true,
            boolean_attributes: Vec::new(),
            implicit_i18n_translate: false,
            implicit_i18n_attributes: Vec::new(),
            trim_attribute_space: false,
            enable_data_attributes: false,
            enable_comment_interpolation: true,
            restricted_namespace: true,
            strict: true,
        }
    }
}

impl Options {
    pub(crate) fn is_boolean(&self, name: &str) -> bool {
        self.boolean_attributes.iter().any(|b| b == name)
    }

    pub(crate) fn is_implicit_i18n(&self, name: &str) -> bool {
        let lower = name.to_lowercase();
        self.implicit_i18n_attributes.contains(&lower)
    }
}
