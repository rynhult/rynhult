/* Test display shim only. Broadway has no XSettings daemon; WebKit treats the
 * backend's default -1 screen DPI literally. Set the fixture to 96 DPI after
 * GTK initializes, before Tauri creates its WebView. Never packaged/shipped. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <gtk/gtk.h>

gboolean gtk_init_check(int *argc, char ***argv) {
    gboolean (*real_init)(int *, char ***) = dlsym(RTLD_NEXT, "gtk_init_check");
    gboolean ok = real_init(argc, argv);
    if (ok) gdk_screen_set_resolution(gdk_screen_get_default(), 96.0);
    return ok;
}
