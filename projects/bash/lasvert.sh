#!/bin/bash

if [[ $1 == *.jpg ]]
then
		tmp=`convert $1 _tmp.bmp`
		for i in {0..9}
		do 
			potrace _tmp.bmp -s -k 0.$i -o ./_test$i.svg
			# echo 0.$i
		done
else
	for i in {0..9}
	do 
		potrace $1 -s -k 0.$i -o ./_test$i.svg
		# echo 0.$i
	done
fi