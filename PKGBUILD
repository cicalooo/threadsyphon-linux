# Maintainer: local <local@localhost>
pkgname=threadsyphon
pkgver=2.0.0
pkgrel=1
pkgdesc="Watch 4chan threads and save their media (GTK4/libadwaita)"
arch=('any')
url="https://github.com/cicalooo/threadsyphon-source"
license=('MIT')
depends=(
  'python'
  'python-gobject'
  'gtk4'
  'libadwaita'
  'libnotify'
)
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-setuptools')
source=()
sha256sums=()

package() {
  cd "$startdir"
  python -m build --wheel --no-isolation
  python -m installer --destdir="$pkgdir" dist/*.whl

  install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
  install -Dm644 data/org.threadsyphon.Threadsyphon.desktop \
    "$pkgdir/usr/share/applications/org.threadsyphon.Threadsyphon.desktop"
  install -Dm644 data/systemd/threadsyphon.service \
    "$pkgdir/usr/lib/systemd/user/threadsyphon.service"

  for size in 16 24 32 48 64 128 256 512; do
    install -Dm644 "data/icons/hicolor/${size}x${size}/apps/threadsyphon.png" \
      "$pkgdir/usr/share/icons/hicolor/${size}x${size}/apps/threadsyphon.png"
  done
}
