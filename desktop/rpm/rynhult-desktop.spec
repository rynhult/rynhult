# Rynhult desktop for RPM distributions: the Linux spike, not a release, and not qualified on any RPM distribution
# yet (docs/LINUX-DISTROS.md). It repackages exactly the files the Tauri .deb installs, so the two packages share one
# build and one layout: desktop/package-rpm.sh extracts that .deb into %%{_sourcedir}/root and runs rpmbuild.
# Unsigned. Licence: Rynhult is MIT; LICENSE and THIRD_PARTY_NOTICES.md ship in the bundled resources' docs/.

# The payload is already built, stripped and byte-compiled. Never let rpmbuild re-strip it, recompile the bundled
# Python's files with the system Python, or rewrite the shebangs of the agent tools.
%global debug_package %{nil}
%global __os_install_post %{nil}
%global __brp_mangle_shebangs %{nil}
# Requirements come from the host binary's ELF libraries (WebKitGTK 4.1, GTK 3), named however this distribution
# names them. The private Python runtime needs nothing from the system, and the package provides nothing to it.
%global __requires_exclude_from ^/usr/lib/[^/]+/desktop/runtime/.*$
%global __provides_exclude_from ^/usr/lib/.*$

# The .deb's own package name and version (its control file), passed by desktop/package-rpm.sh.
Name:           %{rynhult_name}
Version:        %{rynhult_version}
Release:        1%{?dist}
Summary:        Rynhult Linux desktop spike (not a release)
License:        MIT
ExclusiveArch:  x86_64
AutoReqProv:    yes
# Desktop execution setup grants the owner its agent's folders with ACLs (desktop/setup.py checks for setfacl too).
Requires:       /usr/bin/setfacl

%description
The Rynhult desktop window with its bundled local main, Python runtime, catalog and Guide. A development spike:
unsigned, not a release, and not qualified on this distribution.

%prep

%build

%install
cp -a %{_sourcedir}/root/. %{buildroot}/

%files -f %{_sourcedir}/files.list

%changelog
* Sun Oct 25 2026 MAINTAINER PLACEHOLDER - the owner sets the name <maintainer-placeholder@example.invalid> - %{rynhult_version}-1
- Repackaged from the Tauri .deb (spike, not a release)
