#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$SCRIPT_DIR/package"
OUT_DIR="$SCRIPT_DIR/dist"
VER_DIR="$SCRIPT_DIR/versions"
VERSION="1.7.1"
PKG_NAME="openmediavault-agent_${VERSION}_all.deb"

sync_control_version() {
    sed -i "" "s/^Version:.*/Version: $VERSION/" "$PKG_DIR/DEBIAN/control"
}

write_repo_metadata() {
    local pkg_path="$OUT_DIR/$PKG_NAME"
    local rel_path="dist/$PKG_NAME"
    local size md5 sha1 sha256 packages_md5 packages_sha256 packages_gz_md5 packages_gz_sha256
    local packages_size packages_gz_size release_date

    if [ ! -f "$pkg_path" ]; then
        echo "ERROR: Package not found for repo metadata: $pkg_path" >&2
        exit 1
    fi

    size="$(wc -c < "$pkg_path" | tr -d ' ')"
    md5="$(md5sum "$pkg_path" | awk '{print $1}')"
    sha1="$(shasum -a 1 "$pkg_path" | awk '{print $1}')"
    sha256="$(shasum -a 256 "$pkg_path" | awk '{print $1}')"

    {
        sed '/^Filename:/d;/^Size:/d;/^MD5sum:/d;/^SHA1:/d;/^SHA256:/d' "$PKG_DIR/DEBIAN/control"
        echo "Filename: $rel_path"
        echo "Size: $size"
        echo "MD5sum: $md5"
        echo "SHA1: $sha1"
        echo "SHA256: $sha256"
    } > "$SCRIPT_DIR/Packages"

    gzip -c "$SCRIPT_DIR/Packages" > "$SCRIPT_DIR/Packages.gz"

    packages_size="$(wc -c < "$SCRIPT_DIR/Packages" | tr -d ' ')"
    packages_gz_size="$(wc -c < "$SCRIPT_DIR/Packages.gz" | tr -d ' ')"
    packages_md5="$(md5sum "$SCRIPT_DIR/Packages" | awk '{print $1}')"
    packages_gz_md5="$(md5sum "$SCRIPT_DIR/Packages.gz" | awk '{print $1}')"
    packages_sha256="$(shasum -a 256 "$SCRIPT_DIR/Packages" | awk '{print $1}')"
    packages_gz_sha256="$(shasum -a 256 "$SCRIPT_DIR/Packages.gz" | awk '{print $1}')"
    release_date="$(date -u '+%a, %d %b %Y %H:%M:%S +0000')"

    cat > "$SCRIPT_DIR/Release" << EOF
Archive: stable
Component: main
Origin: omv-agent
Label: OMV Agent Helper
Architectures: all arm64 amd64
Date: $release_date
MD5Sum:
 $packages_md5 $packages_size Packages
 $packages_gz_md5 $packages_gz_size Packages.gz
SHA256:
 $packages_sha256 $packages_size Packages
 $packages_gz_sha256 $packages_gz_size Packages.gz
EOF
}

sync_root_artifact() {
    local pkg_path="$OUT_DIR/$PKG_NAME"

    find "$SCRIPT_DIR" -maxdepth 1 -type f -name 'openmediavault-agent_*.deb' -delete
    cp -f "$pkg_path" "$SCRIPT_DIR/$PKG_NAME"
}

build_deb_package() {
    if command -v dpkg-deb >/dev/null 2>&1; then
        dpkg-deb --build --root-owner-group "$PKG_DIR" "$OUT_DIR/$PKG_NAME"
        return
    fi

    if ! command -v ar >/dev/null 2>&1; then
        echo "ERROR: Neither dpkg-deb nor ar is available to build the package." >&2
        exit 1
    fi

    tmpdir="$(mktemp -d)"
    trap 'rm -rf "$tmpdir"' EXIT

    printf '2.0\n' > "$tmpdir/debian-binary"
    
    # Use COPYFILE_DISABLE=1 and --no-xattrs to avoid PAX headers on macOS
    export COPYFILE_DISABLE=1
    
    tar -C "$PKG_DIR/DEBIAN" \
        --uid 0 --gid 0 --uname root --gname root \
        --format=ustar \
        --no-xattrs \
        -czf "$tmpdir/control.tar.gz" .
    tar -C "$PKG_DIR" \
        --exclude ./DEBIAN \
        --exclude '.DS_Store' \
        --uid 0 --gid 0 --uname root --gname root \
        --format=ustar \
        --no-xattrs \
        -czf "$tmpdir/data.tar.gz" .

    rm -f "$OUT_DIR/$PKG_NAME"
    ar -qS "$OUT_DIR/$PKG_NAME" \
        "$tmpdir/debian-binary" \
        "$tmpdir/control.tar.gz" \
        "$tmpdir/data.tar.gz"
}

echo "=== OMV Agent Helper — Build Script ==="
echo ""

# Create dist and versions directories
mkdir -p "$OUT_DIR" "$VER_DIR"

# Backup any existing .deb before overwriting
echo "[0/7] Backing up previous build..."
for old in "$OUT_DIR"/openmediavault-agent_*.deb; do
    [ -f "$old" ] || continue
    base="$(basename "$old" .deb)"
    ts="$(date +%Y%m%d_%H%M%S)"
    cp -f "$old" "$VER_DIR/${base}_backup_${ts}.deb"
    echo "    Backed up: versions/${base}_backup_${ts}.deb"
done

# Copy widget.js to static serving dir
echo "[1/7] Preparing static files..."
mkdir -p "$PKG_DIR/usr/lib/omv-agent/static"
cp -f "$PKG_DIR/usr/lib/omv-agent/widget.js" \
      "$PKG_DIR/usr/lib/omv-agent/static/widget.js"

# Copy knowledge base to package
echo "[2/7] Copying knowledge base..."
if [ -f "$SCRIPT_DIR/knowledge/knowledge_base.json" ]; then
    mkdir -p "$PKG_DIR/usr/share/omv-agent/knowledge"
    cp -f "$SCRIPT_DIR/knowledge/knowledge_base.json" \
          "$PKG_DIR/usr/share/omv-agent/knowledge/knowledge_base.json"
    echo "    Knowledge base included."
else
    echo "    WARNING: knowledge_base.json not found. Creating placeholder."
    mkdir -p "$PKG_DIR/usr/share/omv-agent/knowledge"
    cat > "$PKG_DIR/usr/share/omv-agent/knowledge/knowledge_base.json" << 'EOF'
{
  "version": "1.0",
  "omv_version": "8",
  "entries": []
}
EOF
fi

# Set permissions
echo "[3/7] Setting permissions..."
# DEBIAN scripts must be executable
chmod 755 "$PKG_DIR/DEBIAN/postinst" \
           "$PKG_DIR/DEBIAN/prerm" \
           "$PKG_DIR/DEBIAN/postrm"
# Directories
find "$PKG_DIR" -type d -exec chmod 755 {} \;
# Files (non-DEBIAN)
find "$PKG_DIR" -type f ! -path "*/DEBIAN/*" -exec chmod 644 {} \;
# Python files need read (no execute — run via python3 explicitly)
# Keep DEBIAN scripts executable
chmod 755 "$PKG_DIR/DEBIAN/postinst" \
           "$PKG_DIR/DEBIAN/prerm" \
           "$PKG_DIR/DEBIAN/postrm" \
           "$PKG_DIR/usr/local/bin/berrypi-boot-sequencer"

# Validate control file
echo "[4/7] Validating package structure..."
if [ ! -f "$PKG_DIR/DEBIAN/control" ]; then
    echo "ERROR: DEBIAN/control not found!" >&2
    exit 1
fi

# Check required files exist
REQUIRED=(
    "usr/lib/omv-agent/app.py"
    "usr/lib/omv-agent/brain.py"
    "usr/lib/omv-agent/ollama_bridge.py"
    "usr/lib/omv-agent/probe.py"
    "usr/lib/omv-agent/probe_cache.py"
    "usr/lib/omv-agent/widget.js"
    "usr/lib/omv-agent/static/widget.js"
    "etc/nginx/openmediavault-webgui.d/omv-agent.conf"
    "etc/nginx/conf.d/omv-agent-ratelimit.conf"
    "etc/systemd/system/omv-agent.service"
    "etc/systemd/system/omv-agent-probe.service"
    "etc/systemd/system/omv-agent-watch.service"
    "etc/systemd/system/omv-agent-discover.service"
    "usr/lib/omv-agent/watcher.py"
    "usr/lib/omv-agent/discoverer.py"
    "usr/share/openmediavault/workbench/navigation.d/omv-agent.yaml"
    "usr/share/openmediavault/workbench/route.d/omv-agent.json"
    "usr/local/bin/berrypi-boot-sequencer"
    "etc/systemd/system/berrypi-boot-sequencer.service"
)
for f in "${REQUIRED[@]}"; do
    if [ ! -f "$PKG_DIR/$f" ]; then
        echo "ERROR: Missing required file: $f" >&2
        exit 1
    fi
done
echo "    All required files present."

# Build the .deb
echo "[5/7] Syncing version to DEBIAN/control..."
sync_control_version

echo "[6/7] Building .deb package..."
build_deb_package

echo "[7/8] Refreshing repository metadata..."
write_repo_metadata

echo "[8/8] Syncing root release artifact..."
sync_root_artifact

echo ""
echo "=== BUILD SUCCESSFUL ==="
echo "Package: $OUT_DIR/$PKG_NAME"
echo ""
echo "To install on OMV 8 (ARM64 or AMD64):"
echo "  sudo apt install python3-flask  # if not already installed"
echo "  sudo dpkg -i $OUT_DIR/$PKG_NAME"
echo "  sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo "To verify installation:"
echo "  systemctl status omv-agent"
echo "  curl http://127.0.0.1:11111/health"
echo ""
