#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_DIR="$SCRIPT_DIR/package"
OUT_DIR="$SCRIPT_DIR/dist"
VER_DIR="$SCRIPT_DIR/versions"
VERSION="1.3-1.1"
PKG_NAME="openmediavault-agent_${VERSION}_all.deb"

echo "=== OMV Agent Helper — Build Script ==="
echo ""

# Create dist and versions directories
mkdir -p "$OUT_DIR" "$VER_DIR"

# Backup any existing .deb before overwriting
echo "[0/5] Backing up previous build..."
for old in "$OUT_DIR"/openmediavault-agent_*.deb; do
    [ -f "$old" ] || continue
    base="$(basename "$old" .deb)"
    ts="$(date +%Y%m%d_%H%M%S)"
    cp -f "$old" "$VER_DIR/${base}_backup_${ts}.deb"
    echo "    Backed up: versions/${base}_backup_${ts}.deb"
done

# Copy widget.js to static serving dir
echo "[1/6] Preparing static files..."
mkdir -p "$PKG_DIR/usr/lib/omv-agent/static"
cp -f "$PKG_DIR/usr/lib/omv-agent/widget.js" \
      "$PKG_DIR/usr/lib/omv-agent/static/widget.js"

# Copy knowledge base to package
echo "[2/6] Copying knowledge base..."
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
echo "[3/6] Setting permissions..."
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
           "$PKG_DIR/DEBIAN/postrm"

# Validate control file
echo "[4/6] Validating package structure..."
if [ ! -f "$PKG_DIR/DEBIAN/control" ]; then
    echo "ERROR: DEBIAN/control not found!" >&2
    exit 1
fi

# Check required files exist
REQUIRED=(
    "usr/lib/omv-agent/app.py"
    "usr/lib/omv-agent/brain.py"
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
)
for f in "${REQUIRED[@]}"; do
    if [ ! -f "$PKG_DIR/$f" ]; then
        echo "ERROR: Missing required file: $f" >&2
        exit 1
    fi
done
echo "    All required files present."

# Build the .deb
echo "[5/6] Syncing version to DEBIAN/control..."
sed -i "s/^Version:.*/Version: $VERSION/" "$PKG_DIR/DEBIAN/control"

echo "[6/6] Building .deb package..."
dpkg-deb --build --root-owner-group "$PKG_DIR" "$OUT_DIR/$PKG_NAME"

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
