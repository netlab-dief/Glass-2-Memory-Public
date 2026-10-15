#!/bin/bash

#Change to the directory where the data is located
for img in frames_out/*.jpg; do
        ./full_black_frames_remover "$img"
        case $? in
            0)
                echo "Image $img is fine, keeping it."
                ;;
            1)
                echo "Image $img is completely black, removing it."
                rm "$img"
                ;;
            *)
                echo "An error occurred while processing $img."
                ;;
        esac
    done