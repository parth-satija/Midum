class Cat {
    String name;
    int age;


    public Cat(String name, int age) {
        this.name = name;
        this.age = age;  
    }

    public void displayInfo() {
        System.out.println("Name: " + name);
        System.out.println("Age: " + age);
    }
}
public class Main {
    public static void main(String[] args) {
        Cat cat1 = new Cat("Whiskers", 3);
        Cat cat2 = new Cat("Mittens", 5);
        
        System.out.println("Information about cat1:");
        cat1.displayInfo();
        
        System.out.println("\nInformation about cat2:");
        cat2.displayInfo();
    }
}
