#
# ~/.bash_profile
#

[[ -f ~/.bashrc ]] && . ~/.bashrc


chanify() { ffmpeg -i "$1" -an -fs 3000000 -t 01:59 -c:v vp8 "$1".webm; }


