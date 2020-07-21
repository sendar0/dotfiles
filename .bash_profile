#
# ~/.bash_profile
#

[[ -f ~/.bashrc ]] && . ~/.bashrc


chanify() { ffmpeg -i "$1" -an -fs 3000000 -t 01:59 -c:v vp8 "$1".webm; }
chanify-dl() {
	local title=`youtube-dl --get-filename "$1"`
	youtube-dl -f best "$1" -o -|ffmpeg -i pipe:0 -an -fs 3000000 -t 01:59 -c:v vp8 ./"$title".webm;  }

