#include <fstream>
#include <iostream>
#include <vector>
#include <opencv2/opencv.hpp>

int main(int argc, char* argv[]) {
    if (argc != 2) {
        std::cerr << "Usage: " << argv[0] << " <image.jpg>" << std::endl;
        return 1;
    }

    std::ifstream infile(argv[1], std::ios::binary);
    if (!infile) {
        std::cerr << "Error opening file!" << std::endl;
        return 1;
    }

    std::vector<unsigned char> buffer((std::istreambuf_iterator<char>(infile)), std::istreambuf_iterator<char>());
    infile.close();
    
    cv::Mat img = cv::imdecode(buffer, cv::IMREAD_COLOR);
    if (img.empty()) {
        std::cerr << "Error: Could not decode image." << std::endl;
        return 1;
    }

    cv::Mat gray;
    cv::cvtColor(img, gray, cv::COLOR_BGR2GRAY);
    double minVal, maxVal;
    cv::minMaxLoc(gray, &minVal, &maxVal);
    double meanVal = cv::mean(gray)[0];
    std::cout << "Max pixel value: " << maxVal << std::endl;
    std::cout << "Mean pixel value: " << meanVal << std::endl;
    if (meanVal <= 10) { 
        std::cout << "The image is completely black." << std::endl;
        return 1;
    }
    return 0;

}