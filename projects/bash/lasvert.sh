#!/bin/bash

for i in {0..9}
do 
	potrace $1 -s -k 0.$i -o ./_test$i.svg
	# echo 0.$i
done
