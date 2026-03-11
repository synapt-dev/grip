//! Language and toolchain detection for repositories.
//!
//! Detects the primary language, package manager, and standard build/test/lint/format
//! commands by checking for marker files (e.g., `Cargo.toml`, `package.json`).

use std::path::Path;

/// Detected language and toolchain for a repository.
#[derive(Debug, Clone)]
pub struct DetectedToolchain {
    /// Primary language (e.g., "rust", "typescript", "python", "go")
    pub language: String,
    /// Package manager (e.g., "cargo", "pnpm", "yarn", "npm", "uv", "poetry")
    pub package_manager: Option<String>,
    /// Build command
    pub build: Option<String>,
    /// Test command
    pub test: Option<String>,
    /// Lint command
    pub lint: Option<String>,
    /// Format command
    pub format: Option<String>,
    /// Install/setup command (for post-sync hooks)
    pub install: Option<String>,
}

/// Detect the primary language and toolchain for a repository at the given path.
///
/// Returns `None` if no recognized marker files are found.
/// Uses file existence checks only — no file parsing.
pub fn detect_toolchain(repo_path: &Path) -> Option<DetectedToolchain> {
    if repo_path.join("Cargo.toml").exists() {
        return Some(detect_rust());
    }

    if repo_path.join("package.json").exists() {
        return Some(detect_javascript(repo_path));
    }

    if repo_path.join("pyproject.toml").exists() || repo_path.join("setup.py").exists() {
        return Some(detect_python(repo_path));
    }

    if repo_path.join("go.mod").exists() {
        return Some(detect_go(repo_path));
    }

    if repo_path.join("Gemfile").exists() {
        return Some(detect_ruby(repo_path));
    }

    if repo_path.join("build.gradle").exists()
        || repo_path.join("build.gradle.kts").exists()
        || repo_path.join("pom.xml").exists()
    {
        return Some(detect_java(repo_path));
    }

    if repo_path.join("CMakeLists.txt").exists() {
        return Some(DetectedToolchain {
            language: "cpp".to_string(),
            package_manager: Some("cmake".to_string()),
            build: Some("cmake --build build".to_string()),
            test: Some("ctest --test-dir build".to_string()),
            lint: None,
            format: None,
            install: None,
        });
    }

    if repo_path.join("Makefile").exists() {
        return Some(DetectedToolchain {
            language: "c".to_string(),
            package_manager: Some("make".to_string()),
            build: Some("make".to_string()),
            test: Some("make test".to_string()),
            lint: None,
            format: None,
            install: None,
        });
    }

    None
}

fn detect_rust() -> DetectedToolchain {
    DetectedToolchain {
        language: "rust".to_string(),
        package_manager: Some("cargo".to_string()),
        build: Some("cargo build".to_string()),
        test: Some("cargo test".to_string()),
        lint: Some("cargo clippy".to_string()),
        format: Some("cargo fmt".to_string()),
        install: None,
    }
}

fn detect_javascript(repo_path: &Path) -> DetectedToolchain {
    let pm = detect_js_package_manager(repo_path);
    let language = if repo_path.join("tsconfig.json").exists() {
        "typescript"
    } else {
        "javascript"
    };
    DetectedToolchain {
        language: language.to_string(),
        package_manager: Some(pm.clone()),
        build: Some(format!("{pm} run build")),
        test: Some(format!("{pm} test")),
        lint: Some(format!("{pm} run lint")),
        format: Some(format!("{pm} run format")),
        install: Some(format!("{pm} install")),
    }
}

fn detect_js_package_manager(repo_path: &Path) -> String {
    if repo_path.join("pnpm-lock.yaml").exists() {
        "pnpm".to_string()
    } else if repo_path.join("yarn.lock").exists() {
        "yarn".to_string()
    } else if repo_path.join("bun.lockb").exists() || repo_path.join("bun.lock").exists() {
        "bun".to_string()
    } else {
        "npm".to_string()
    }
}

fn detect_python(repo_path: &Path) -> DetectedToolchain {
    let (pm, install) = detect_python_package_manager(repo_path);
    DetectedToolchain {
        language: "python".to_string(),
        package_manager: Some(pm),
        build: None,
        test: Some("pytest".to_string()),
        lint: Some("ruff check .".to_string()),
        format: Some("ruff format .".to_string()),
        install: Some(install),
    }
}

fn detect_python_package_manager(repo_path: &Path) -> (String, String) {
    if repo_path.join("uv.lock").exists() {
        ("uv".to_string(), "uv sync".to_string())
    } else if repo_path.join("poetry.lock").exists() {
        ("poetry".to_string(), "poetry install".to_string())
    } else if repo_path.join("Pipfile.lock").exists() || repo_path.join("Pipfile").exists() {
        ("pipenv".to_string(), "pipenv install".to_string())
    } else {
        ("pip".to_string(), "pip install -e .".to_string())
    }
}

fn detect_go(repo_path: &Path) -> DetectedToolchain {
    let has_golangci =
        repo_path.join(".golangci.yml").exists() || repo_path.join(".golangci.yaml").exists();

    DetectedToolchain {
        language: "go".to_string(),
        package_manager: Some("go".to_string()),
        build: Some("go build ./...".to_string()),
        test: Some("go test ./...".to_string()),
        lint: if has_golangci {
            Some("golangci-lint run".to_string())
        } else {
            None
        },
        format: Some("gofmt -w .".to_string()),
        install: None,
    }
}

fn detect_ruby(repo_path: &Path) -> DetectedToolchain {
    let has_rakefile = repo_path.join("Rakefile").exists();
    DetectedToolchain {
        language: "ruby".to_string(),
        package_manager: Some("bundler".to_string()),
        build: if has_rakefile {
            Some("bundle exec rake build".to_string())
        } else {
            None
        },
        test: Some("bundle exec rspec".to_string()),
        lint: None,
        format: None,
        install: Some("bundle install".to_string()),
    }
}

fn detect_java(repo_path: &Path) -> DetectedToolchain {
    if repo_path.join("build.gradle").exists() || repo_path.join("build.gradle.kts").exists() {
        DetectedToolchain {
            language: "java".to_string(),
            package_manager: Some("gradle".to_string()),
            build: Some("./gradlew build".to_string()),
            test: Some("./gradlew test".to_string()),
            lint: None,
            format: None,
            install: None,
        }
    } else {
        DetectedToolchain {
            language: "java".to_string(),
            package_manager: Some("maven".to_string()),
            build: Some("mvn package".to_string()),
            test: Some("mvn test".to_string()),
            lint: None,
            format: None,
            install: None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn create_file(dir: &Path, name: &str) {
        std::fs::write(dir.join(name), "").unwrap();
    }

    #[test]
    fn test_detect_rust() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "Cargo.toml");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "rust");
        assert_eq!(tc.package_manager.as_deref(), Some("cargo"));
        assert_eq!(tc.build.as_deref(), Some("cargo build"));
        assert_eq!(tc.test.as_deref(), Some("cargo test"));
        assert_eq!(tc.lint.as_deref(), Some("cargo clippy"));
        assert_eq!(tc.format.as_deref(), Some("cargo fmt"));
        assert!(tc.install.is_none());
    }

    #[test]
    fn test_detect_typescript_pnpm() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "package.json");
        create_file(dir.path(), "pnpm-lock.yaml");
        create_file(dir.path(), "tsconfig.json");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "typescript");
        assert_eq!(tc.package_manager.as_deref(), Some("pnpm"));
        assert_eq!(tc.build.as_deref(), Some("pnpm run build"));
        assert_eq!(tc.install.as_deref(), Some("pnpm install"));
    }

    #[test]
    fn test_detect_javascript_yarn() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "package.json");
        create_file(dir.path(), "yarn.lock");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "javascript");
        assert_eq!(tc.package_manager.as_deref(), Some("yarn"));
    }

    #[test]
    fn test_detect_javascript_bun() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "package.json");
        create_file(dir.path(), "bun.lockb");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.package_manager.as_deref(), Some("bun"));
    }

    #[test]
    fn test_detect_javascript_bun_text_lockfile() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "package.json");
        create_file(dir.path(), "bun.lock");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.package_manager.as_deref(), Some("bun"));
    }

    #[test]
    fn test_detect_javascript_npm_fallback() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "package.json");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "javascript");
        assert_eq!(tc.package_manager.as_deref(), Some("npm"));
    }

    #[test]
    fn test_detect_typescript_with_tsconfig() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "package.json");
        create_file(dir.path(), "tsconfig.json");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "typescript");
        assert_eq!(tc.package_manager.as_deref(), Some("npm"));
    }

    #[test]
    fn test_detect_python_uv() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "pyproject.toml");
        create_file(dir.path(), "uv.lock");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "python");
        assert_eq!(tc.package_manager.as_deref(), Some("uv"));
        assert_eq!(tc.install.as_deref(), Some("uv sync"));
        assert_eq!(tc.test.as_deref(), Some("pytest"));
    }

    #[test]
    fn test_detect_python_poetry() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "pyproject.toml");
        create_file(dir.path(), "poetry.lock");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.package_manager.as_deref(), Some("poetry"));
        assert_eq!(tc.install.as_deref(), Some("poetry install"));
    }

    #[test]
    fn test_detect_python_pipenv() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "pyproject.toml");
        create_file(dir.path(), "Pipfile.lock");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.package_manager.as_deref(), Some("pipenv"));
    }

    #[test]
    fn test_detect_python_pip_fallback() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "setup.py");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "python");
        assert_eq!(tc.package_manager.as_deref(), Some("pip"));
        assert_eq!(tc.install.as_deref(), Some("pip install -e ."));
    }

    #[test]
    fn test_detect_go() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "go.mod");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "go");
        assert_eq!(tc.build.as_deref(), Some("go build ./..."));
        assert_eq!(tc.test.as_deref(), Some("go test ./..."));
        assert!(tc.lint.is_none());
        assert!(tc.install.is_none());
    }

    #[test]
    fn test_detect_go_with_golangci() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "go.mod");
        create_file(dir.path(), ".golangci.yml");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.lint.as_deref(), Some("golangci-lint run"));
    }

    #[test]
    fn test_detect_ruby() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "Gemfile");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "ruby");
        assert_eq!(tc.package_manager.as_deref(), Some("bundler"));
        assert!(tc.build.is_none());
        assert_eq!(tc.test.as_deref(), Some("bundle exec rspec"));
        assert_eq!(tc.install.as_deref(), Some("bundle install"));
    }

    #[test]
    fn test_detect_ruby_with_rakefile() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "Gemfile");
        create_file(dir.path(), "Rakefile");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.build.as_deref(), Some("bundle exec rake build"));
    }

    #[test]
    fn test_detect_java_gradle() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "build.gradle");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "java");
        assert_eq!(tc.package_manager.as_deref(), Some("gradle"));
        assert_eq!(tc.build.as_deref(), Some("./gradlew build"));
    }

    #[test]
    fn test_detect_java_gradle_kts() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "build.gradle.kts");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "java");
        assert_eq!(tc.package_manager.as_deref(), Some("gradle"));
    }

    #[test]
    fn test_detect_java_maven() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "pom.xml");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "java");
        assert_eq!(tc.package_manager.as_deref(), Some("maven"));
        assert_eq!(tc.build.as_deref(), Some("mvn package"));
    }

    #[test]
    fn test_detect_cpp_cmake() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "CMakeLists.txt");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "cpp");
        assert_eq!(tc.package_manager.as_deref(), Some("cmake"));
    }

    #[test]
    fn test_detect_c_makefile() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "Makefile");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "c");
        assert_eq!(tc.package_manager.as_deref(), Some("make"));
    }

    #[test]
    fn test_detect_none_empty_dir() {
        let dir = TempDir::new().unwrap();
        assert!(detect_toolchain(dir.path()).is_none());
    }

    #[test]
    fn test_priority_rust_over_makefile() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "Cargo.toml");
        create_file(dir.path(), "Makefile");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "rust");
    }

    #[test]
    fn test_priority_rust_over_javascript() {
        let dir = TempDir::new().unwrap();
        create_file(dir.path(), "Cargo.toml");
        create_file(dir.path(), "package.json");
        let tc = detect_toolchain(dir.path()).unwrap();
        assert_eq!(tc.language, "rust");
    }
}
