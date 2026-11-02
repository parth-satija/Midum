import java.util.*;
public class Main {
    public int getScore() {
        boolean bonus = isBonus();
        boolean g1 = levelOne.goalReached();
        boolean g2 = levelTwo.goalReached();
        boolean g3 = levelThree.goalReached();
        int score = 0;
        if (g1) {
            score += levelOne.getPoints();
            if (g2) {
                score += levelTwo.getPoints();
                if (g3) {
                    score += levelThree.getPoints();
                }
                if (bonus) {
                    score *= 3;
                }
            }
        }
        return score;
    }

}