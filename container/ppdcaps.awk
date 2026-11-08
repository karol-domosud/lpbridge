# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Karol Domosud

# ppdcaps.awk -- extract the option groups the agent maps to IPP attributes.
#
# Emits JSON on stdout:
#   {"ppd":"<path>","nickname":"<NickName>",
#    "options":{"PageSize":{"default":"Letter",
#                           "choices":{"Letter":"US Letter", ...}}, ...}}
#
# Only the options in WANT are reported. gawk is
# required for 3-argument match().

BEGIN {
    split("PageSize Duplex ColorModel Resolution InputSlot MediaType OutputBin", w, " ")
    for (i in w) want[w[i]] = 1
    cur = ""
    nickname = ""
}

function jesc(s,    r) {
    r = s
    gsub(/\\/, "\\\\", r)
    gsub(/"/, "\\\"", r)
    gsub(/\t/, " ", r)
    gsub(/\r/, "", r)
    return r
}

/^\*NickName:/ {
    if (match($0, /"([^"]*)"/, mm)) nickname = mm[1]
    next
}
/^\*ModelName:/ {
    if (nickname == "" && match($0, /"([^"]*)"/, mm)) nickname = mm[1]
    next
}

/^\*OpenUI \*/ {
    if (match($0, /^\*OpenUI \*([A-Za-z0-9_]+)/, mm)) {
        cur = mm[1]
        if (cur in want && !(cur in seen)) { seen[cur] = 1 }
    }
    next
}
/^\*CloseUI:/ { cur = ""; next }
/^\*JCLOpenUI \*/ {
    if (match($0, /^\*JCLOpenUI \*([A-Za-z0-9_]+)/, mm)) cur = mm[1]
    next
}
/^\*JCLCloseUI:/ { cur = ""; next }

/^\*Default[A-Za-z0-9_]+:/ {
    opt = $1
    sub(/^\*Default/, "", opt)
    sub(/:$/, "", opt)
    if (opt in want) {
        val = $2
        sub(/^[[:space:]]+/, "", val)
        defaults[opt] = val
        seen[opt] = 1
    }
    next
}

{
    if (cur != "" && (cur in want) && index($0, "*" cur " ") == 1) {
        rest = substr($0, length(cur) + 3)
        cpos = index(rest, ":")
        if (cpos > 0) {
            keypart = substr(rest, 1, cpos - 1)
        } else {
            keypart = rest
        }
        sub(/[[:space:]]+$/, "", keypart)
        spos = index(keypart, "/")
        if (spos > 0) {
            key = substr(keypart, 1, spos - 1)
            tr  = substr(keypart, spos + 1)
        } else {
            key = keypart
            tr  = keypart
        }
        if (key != "" && key !~ /[[:space:]]/ && !((cur SUBSEP key) in choiceseen)) {
            choiceseen[cur SUBSEP key] = 1
            choices[cur, key] = tr
            order[cur] = order[cur] (order[cur] == "" ? "" : "\x1e") key
            seen[cur] = 1
        }
    }
}

END {
    printf "{"
    printf "\"ppd\":\"%s\",", jesc(ppd_path)
    printf "\"nickname\":\"%s\",", jesc(nickname == "" ? "Generic Printer" : nickname)
    printf "\"options\":{"
    first_opt = 1
    for (o in seen) {
        if (!(o in want)) continue
        if (order[o] == "" && !(o in defaults)) continue
        if (!first_opt) printf ","
        first_opt = 0
        printf "\"%s\":{", jesc(o)
        printf "\"default\":%s", (o in defaults) ? ("\"" jesc(defaults[o]) "\"") : "null"
        printf ",\"choices\":{"
        n = split(order[o], ks, "\x1e")
        for (k = 1; k <= n; k++) {
            if (ks[k] == "") continue
            if (k > 1) printf ","
            printf "\"%s\":\"%s\"", jesc(ks[k]), jesc(choices[o, ks[k]])
        }
        printf "}}"
    }
    printf "}}"
    printf "\n"
}
