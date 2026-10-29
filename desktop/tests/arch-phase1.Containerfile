# Resolved and qualified on 2026-10-25; all package changes are confined to this guest.
FROM docker.io/library/archlinux@sha256:52dfbd5afb23a220d5ee92dafcb4c788d96c026ed04179890c23adef4c897fa0
ENV container=podman
RUN pacman -Syu --noconfirm --needed base-devel systemd dbus acl git tmux nodejs npm python procps-ng cronie webkit2gtk-4.1 gtk3 libayatana-appindicator librsvg && pacman -Scc --noconfirm
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
