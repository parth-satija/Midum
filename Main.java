import java.util.*;
public class Main {
    public static void main(String[] args) {
        String[] words = {"apple", "banana", "cherry", "date", "elderberry", "fig", "grape", "honeydew", "kiwi", "lemon"};
        String[] evenWords = new String[words.length / 2];
        String[] oddWords = new String[words.length / 2];
        String[] alternatedWords = new String[words.length];
        int c = 0;
        for(String word: words) {
            if (c % 2 == 0){
                evenWords[c/2] = word;
            }
            else {
                oddWords[c/2] = word;
            }
            c++;
        }
        for (int i = 0; i < words.length / 2; i++) {
            alternatedWords[i * 2] = oddWords[i];
            alternatedWords[i * 2 + 1] = evenWords[i];
        }
        System.out.println(Arrays.toString(alternatedWords));
    }

}