# shellcheck shell=sh
# Shared POSIX-shell environment compatibility. Source before reading settings.
# Only validated variable names enter eval; values are expanded as data, never code.
acc_env_compat() {
    acc_compat_present='' acc_compat_value=''
    for acc_compat_key in $(env | sed -n -e 's/^\(ACC_[A-Z0-9_]*\)=.*/\1/p' -e 's/^\(RYNHULT_[A-Z0-9_]*\)=.*/\1/p'); do
        case "$acc_compat_key" in
            ACC_*) acc_compat_suffix=${acc_compat_key#ACC_} ;;
            RYNHULT_*) acc_compat_suffix=${acc_compat_key#RYNHULT_} ;;
        esac
        acc_compat_new=RYNHULT_$acc_compat_suffix
        acc_compat_old=ACC_$acc_compat_suffix
        eval 'acc_compat_present=${'"$acc_compat_new"'+x}'
        if [ "$acc_compat_present" = x ]; then
            eval 'acc_compat_value=${'"$acc_compat_new"'}'
        else
            eval 'acc_compat_present=${'"$acc_compat_old"'+x}'
            [ "$acc_compat_present" = x ] || continue
            eval 'acc_compat_value=${'"$acc_compat_old"'}'
        fi
        export "$acc_compat_old=$acc_compat_value" "$acc_compat_new=$acc_compat_value"
    done
    unset acc_compat_key acc_compat_suffix acc_compat_new acc_compat_old acc_compat_present acc_compat_value
}
acc_env_compat

# Public command aliases obey the same no-overwrite rule as unit aliases.
acc_safe_link() {
    if [ -L "$2" ]; then
        [ "$(readlink "$2")" = "$1" ] && return 0
        printf 'Alias conflict: %s (review existing link)\n' "$2" >&2
        return 1
    fi
    if [ -e "$2" ]; then
        printf 'Alias conflict: %s (refusing to overwrite)\n' "$2" >&2
        return 1
    fi
    ln -s "$1" "$2"
}

# Read the one authoritative env file as data (never source it as shell code).
# Preserve the existing raw-value format and last-assignment behavior.
acc_setting_file() {
    case "$1" in *[!A-Z0-9_]*|'') return 2 ;; esac
    [ -f "$2" ] || return 0
    case "$1" in
        ACC_*) acc_setting_suffix=${1#ACC_} ;;
        RYNHULT_*) acc_setting_suffix=${1#RYNHULT_} ;;
        *) return 2 ;;
    esac
    if grep -q "^RYNHULT_$acc_setting_suffix=" "$2"; then
        sed -n "s/^RYNHULT_$acc_setting_suffix=//p" "$2" | tail -1
    else
        sed -n "s/^ACC_$acc_setting_suffix=//p" "$2" | tail -1
    fi
    unset acc_setting_suffix
}
