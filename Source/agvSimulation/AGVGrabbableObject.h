
#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "AGVGrabbableObject.generated.h"

class UStaticMeshComponent;
class USceneComponent;

// 상품 종류
UENUM(BlueprintType)
enum class EProductType : uint8
{
    Red   UMETA(DisplayName = "Red"),
    Green UMETA(DisplayName = "Green"),
    Blue  UMETA(DisplayName = "Blue")
};

UCLASS()
class AGVSIMULATION_API AAGVGrabbableObject : public AActor
{
    GENERATED_BODY()

public:
    AAGVGrabbableObject();

    // 상품 종류
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Product")
    EProductType ProductType = EProductType::Red;

    // 현재 집혀 있는지
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Grab")
    bool bIsGrabbed = false;

    // 물체 집기
    UFUNCTION(BlueprintCallable, Category = "Grab")
    void Grab(USceneComponent* AttachPoint);

    // 물체 놓기
    UFUNCTION(BlueprintCallable, Category = "Grab")
    void Release();

    // 강조 표시 활성화 / 비활성화
    UFUNCTION(BlueprintCallable, Category = "Grab")
    void SetHighlighted(bool bHighlighted);

    UFUNCTION(BlueprintPure, Category = "Product")
    EProductType GetProductType() const;

    virtual void Tick(float DeltaTime) override;

protected:
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Components")
    UStaticMeshComponent* ObjectMesh;

    // 집기 가능 상태 표시
    UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category = "Interaction")
    bool bIsHighlighted = false;

    // 원형 링 반지름
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "Interaction")
    float HighlightRadius = 45.0f;

};
