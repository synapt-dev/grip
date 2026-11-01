//! gr2_gix: the reads one check-store invocation makes, answered in-process by gix instead of one git
//! process per fact. Every answer is shaped exactly as the git command it replaces prints it, so the
//! Python side compares bytes, not meanings. Anything this reader cannot answer the way git would is
//! reported as `None` (or a `LookupError`), and the caller asks git instead.
use std::path::PathBuf;

use gix::objs::tree::EntryKind;
use gix::ObjectId;
use pyo3::exceptions::{PyLookupError, PyOSError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

fn oid(hex: &str) -> PyResult<ObjectId> {
    ObjectId::from_hex(hex.as_bytes()).map_err(|e| PyValueError::new_err(format!("{hex:?} is not an object id: {e}")))
}

fn os(e: impl std::fmt::Display) -> PyErr {
    PyOSError::new_err(e.to_string())
}

fn mode(kind: EntryKind) -> (&'static str, &'static str) {
    // (mode, type) exactly as `git ls-tree` prints them
    match kind {
        EntryKind::Tree => ("040000", "tree"),
        EntryKind::Blob => ("100644", "blob"),
        EntryKind::BlobExecutable => ("100755", "blob"),
        EntryKind::Link => ("120000", "blob"),
        EntryKind::Commit => ("160000", "commit"),
    }
}

#[pyclass(unsendable)]
struct Reader {
    repo: gix::Repository,
}

impl Reader {
    fn walk(&self, tree: ObjectId, prefix: &[u8], out: &mut Vec<u8>) -> PyResult<()> {
        let object = self.repo.find_object(tree).map_err(os)?;
        if object.kind != gix::object::Kind::Tree {
            return Err(PyValueError::new_err(format!("{tree} is {}, not a tree", object.kind)));
        }
        let data = object.data.clone();
        for entry in gix::objs::TreeRefIter::from_bytes(&data, self.repo.object_hash()) {
            let entry = entry.map_err(os)?;
            let (m, t) = mode(entry.mode.kind());
            let mut path = prefix.to_vec();
            path.extend_from_slice(entry.filename);
            out.extend_from_slice(format!("{m} {t} {}\t", entry.oid).as_bytes());
            out.extend_from_slice(&path);
            out.push(0);
            // git lists a subtree's entries right after the subtree's own line
            if entry.mode.kind() == EntryKind::Tree {
                path.push(b'/');
                self.walk(entry.oid.to_owned(), &path, out)?;
            }
        }
        Ok(())
    }
}

#[pymethods]
impl Reader {
    #[new]
    fn new(path: PathBuf) -> PyResult<Self> {
        let repo = gix::open(&path).map_err(os)?;
        Ok(Reader { repo })
    }

    /// `git rev-parse --show-object-format`: "sha1" or "sha256".
    fn object_format(&self) -> &'static str {
        match self.repo.object_hash() {
            gix::hash::Kind::Sha1 => "sha1",
            #[allow(unreachable_patterns)]
            _ => "sha256",
        }
    }

    /// `git cat-file -t <oid>`: the object's type, or None when the object is absent.
    fn kind(&self, hex: &str) -> PyResult<Option<String>> {
        let id = oid(hex)?;
        match self.repo.try_find_header(id).map_err(os)? {
            Some(header) => Ok(Some(header.kind().to_string())),
            None => Ok(None),
        }
    }

    /// `git rev-parse --verify <ref>` for a full ref name: the id a direct ref names, unpeeled, so a ref at an
    /// annotated tag answers the tag's id, as git does. None when the ref is absent. A name that is not a full
    /// ref, or a symbolic ref, raises LookupError, and the caller asks git.
    fn rev_parse_ref(&self, name: &str) -> PyResult<Option<String>> {
        if !name.starts_with("refs/") {
            return Err(PyLookupError::new_err("not a full ref name"));
        }
        let found = self.repo.try_find_reference(name).map_err(os)?;
        let Some(reference) = found else { return Ok(None) };
        match reference.target().try_id() {
            Some(id) => Ok(Some(id.to_string())),
            None => Err(PyLookupError::new_err("a symbolic ref")),
        }
    }

    /// `git ls-tree -r -t -z <tree-ish>`, byte for byte. Like git, a commit or a tag is read as its tree.
    fn ls_tree<'py>(&self, py: Python<'py>, hex: &str) -> PyResult<Bound<'py, PyBytes>> {
        let object = self.repo.find_object(oid(hex)?).map_err(os)?;
        let tree = object.peel_to_tree().map_err(|e| PyValueError::new_err(e.to_string()))?.id;
        let mut out = Vec::new();
        self.walk(tree, b"", &mut out)?;
        Ok(PyBytes::new(py, &out))
    }

    /// The header of an object as `cat-file --batch` reports it: (type, size), or None when absent.
    fn header(&self, hex: &str) -> PyResult<Option<(String, u64)>> {
        let id = oid(hex)?;
        Ok(self.repo.try_find_header(id).map_err(os)?.map(|h| (h.kind().to_string(), h.size())))
    }

    /// A blob's body. The caller checks the header (type, size limit) first and re-hashes the body.
    fn blob<'py>(&self, py: Python<'py>, hex: &str) -> PyResult<Bound<'py, PyBytes>> {
        let object = self.repo.find_object(oid(hex)?).map_err(os)?;
        if object.kind != gix::object::Kind::Blob {
            return Err(PyValueError::new_err(format!("{hex} is {}, not a blob", object.kind)));
        }
        Ok(PyBytes::new(py, &object.data))
    }
}

#[pymodule]
fn gr2_gix(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<Reader>()?;
    Ok(())
}
